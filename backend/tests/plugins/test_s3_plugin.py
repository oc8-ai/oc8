"""The shipped S3 plugin, against an in-process fake S3.

Nothing here reaches AWS. The SigV4 signing is checked against the AWS
documentation's own known-answer vector, not merely "a header was produced".
"""

from __future__ import annotations

import base64
import datetime as dt
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from oc8.capas import contributions, loader
from oc8.capas.discovery import find_plugin
from oc8.config import get_settings
from oc8.knowledge.connectors.base import ConnectorError
from oc8.oauth import http as oauth_http

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"

CFG = {
    "bucket": "my-bucket",
    "credential": "test-credential-id",
}


class _Secrets:
    """An AuthContext that only serves secrets/credentials — S3 uses no OAuth."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {"s3/access": "AKIAEXAMPLE", "s3/secret": "shhh"}
        # Keyed by (credential_id, field_key) -- what `auth.credential(...)`
        # actually resolves for CFG's `"credential": "test-credential-id"`,
        # mirroring what a real `Credential` row of type `s3_api` holds
        # (capas/s3_source/credential_types/s3_api.toml).
        self.credentials: dict[tuple[str, str], str] = {
            ("test-credential-id", "access_key"): "AKIAEXAMPLE",
            ("test-credential-id", "secret_key"): "shhh",
            ("test-credential-id", "region"): "eu-central-1",
            ("test-credential-id", "endpoint"): "",
        }
        self.asked: list[str] = []
        self.asked_credentials: list[tuple[str, str]] = []

    async def token(self) -> str:
        raise ConnectorError("this source has no connected account")

    async def secret(self, ref: str) -> str:
        self.asked.append(ref)
        try:
            return self.values[ref]
        except KeyError:
            raise ConnectorError(f"no stored secret named {ref!r}") from None

    async def credential(self, credential_id: str, field_key: str) -> str:
        self.asked_credentials.append((credential_id, field_key))
        try:
            return self.credentials[(credential_id, field_key)]
        except KeyError:
            raise ConnectorError(
                f"no credential {credential_id!r} field {field_key!r}"
            ) from None


def _list_xml(keys: list[tuple[str, int]]) -> str:
    items = "".join(
        f"<Contents><Key>{k}</Key><Size>{s}</Size></Contents>" for k, s in keys
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"{items}</ListBucketResult>"
    )


class _S3:
    def __init__(self, keys: list[tuple[str, int]], bodies: dict[str, str] | None = None) -> None:
        self.keys = keys
        self.bodies = bodies or {}
        self.requests: list[httpx.Request] = []
        self.status_override: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_override is not None:
            return httpx.Response(self.status_override, text="<Error/>")
        if request.url.params.get("list-type") == "2":
            return httpx.Response(200, text=_list_xml(self.keys))
        key = request.url.path.lstrip("/")
        if key.startswith("my-bucket/"):
            key = key[len("my-bucket/") :]
        return httpx.Response(200, text=self.bodies.get(key, ""))


def _evict() -> None:
    """Drop every cached `connector`/`connector.*` module from sys.modules.

    s3_source's package is called `connector` after the restructure -- a name
    gdrive_source, microsoft365 and google_workspace all also ship (design §2)
    -- and sys.modules is keyed by NAME, not by path. Called SYMMETRICALLY on
    both sides of `_plugin_package_path`'s yield: before, so a sibling's cached
    copy cannot answer our import; after, so nothing generic is left cached for
    anyone else. The trailing half is the load-bearing one --
    `loader.import_entry_point` (Task 6's collision fix) only evicts modules IT
    ITSELF introduced, so a `connector` left cached here would make a later
    `find_plugin`/`load_plugin` for gdrive_source silently hand back THIS
    plugin's `register`, in this very directory (test_gdrive_plugin.py and
    test_connector_attestation.py are collected right beside this file).
    """
    for _stale in [n for n in sys.modules if n == "connector" or n.startswith("connector.")]:
        del sys.modules[_stale]


@pytest.fixture()
def _plugin_package_path() -> Iterator[None]:
    """Make s3_source's `connector` package the one that resolves, for one test.

    Task 7's `_plugin_path` convention, renamed in this one file only so it is
    not confused with the pre-existing `_plugins_path` fixture directly below
    (which points OC8_CAPAS_PATH at the plugins dir and resets the loader --
    a different job entirely). Deliberately NOT autouse, per Task 7 §2e: exactly
    ONE of this file's 20 tests imports the plugin package directly; every other
    test reaches the connector through the real loader, and evicting/re-importing
    for all of them would be pure overhead. The one test requests it by name,
    which gives the same ordering guarantee autouse would.
    """
    _evict()
    sys.path.insert(0, str(PLUGINS_DIR / "s3_source"))
    yield
    sys.path.remove(str(PLUGINS_DIR / "s3_source"))
    _evict()


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(PLUGINS_DIR))
    get_settings.cache_clear()
    loader.reset_for_tests()
    contributions.reset_for_tests()
    yield
    oauth_http.set_transport_override(None)
    loader.reset_for_tests()
    contributions.reset_for_tests()
    get_settings.cache_clear()


def _connector() -> Any:
    found = find_plugin("s3_source")
    assert found is not None, "s3_source plugin not discovered"
    assert found.valid, found.error
    assert loader.load_plugin(found) is True
    return contributions.connectors_for("s3_source")["s3"]


def _install(s3: _S3) -> None:
    oauth_http.set_transport_override(httpx.MockTransport(s3.handler))


async def _collect(c: Any, config: dict[str, Any], cursor: Any, auth: Any) -> list[Any]:
    return [d async for d in c.fetch(config, cursor, auth)]


# ------------------------------------------------------------------ signing

async def test_sigv4_matches_the_aws_known_answer_vector(_plugin_package_path: None) -> None:
    """AWS's own 'get-vanilla' SigV4 test case. This pins the signing algorithm
    itself -- a self-consistent implementation that AWS rejects would otherwise
    look fine in every other test here.

    `_plugin_package_path` is requested for its side effect, not its value: it
    is what makes the bare name `connector` below mean s3_source's package and
    nothing else, and what cleans it back out of sys.modules afterwards."""
    from connector.connector import sign_request  # type: ignore[import-not-found]

    headers = sign_request(
        method="GET",
        host="example.amazonaws.com",
        path="/",
        query="",
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        now=dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.UTC),
    )
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/s3/")
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert headers["x-amz-date"] == "20150830T123600Z"
    # Deterministic: the same inputs must always yield the same signature.
    again = sign_request(
        method="GET",
        host="example.amazonaws.com",
        path="/",
        query="",
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        now=dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.UTC),
    )
    assert again["Authorization"] == auth
    # And the signature must actually depend on the secret.
    other = sign_request(
        method="GET",
        host="example.amazonaws.com",
        path="/",
        query="",
        access_key="AKIDEXAMPLE",
        secret_key="a-different-secret",
        region="us-east-1",
        now=dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.UTC),
    )
    assert other["Authorization"] != auth


# ------------------------------------------------------------------ credentials

async def test_credentials_come_from_the_credential_not_the_config() -> None:
    """The whole point: the source config names a credential, it never holds
    the keys/region/endpoint themselves."""
    s3 = _S3([])
    _install(s3)
    auth = _Secrets()
    await _connector().discover(CFG, auth)
    assert auth.asked_credentials == [
        ("test-credential-id", "access_key"),
        ("test-credential-id", "secret_key"),
        ("test-credential-id", "region"),
        ("test-credential-id", "endpoint"),
    ]
    # The key must never appear in the config we would persist.
    assert "AKIAEXAMPLE" not in str(CFG)


async def test_a_missing_credential_field_is_a_clear_config_error() -> None:
    s3 = _S3([])
    _install(s3)
    auth = _Secrets()
    del auth.credentials[("test-credential-id", "secret_key")]
    res = await _connector().validate(CFG, auth)
    assert res.ok is False
    assert "secret_key" in (res.error or "")


async def test_a_missing_credential_is_rejected() -> None:
    res = await _connector().validate({"bucket": "b"}, _Secrets())
    assert res.ok is False
    assert "credential" in (res.error or "")


async def test_a_bucket_is_required() -> None:
    res = await _connector().validate({"credential": "test-credential-id"}, _Secrets())
    assert res.ok is False
    assert "bucket" in (res.error or "")


async def test_a_prefix_identical_to_the_bucket_name_is_refused() -> None:
    # Live, 2026-08-21: a user typed the bucket name into prefix too, and the
    # source listed 0 objects forever with no error -- indistinguishable from
    # an honestly-empty bucket. Caught before it ever reaches _list().
    res = await _connector().validate(
        {**CFG, "bucket": "oc8-test2", "prefix": "oc8-test2"}, _Secrets()
    )
    assert res.ok is False
    assert "prefix" in (res.error or "").lower()
    assert "bucket name" in (res.error or "")


async def test_a_prefix_that_differs_from_the_bucket_name_is_fine() -> None:
    s3 = _S3([("reports/q1.txt", 5)], {"reports/q1.txt": "hello"})
    _install(s3)
    res = await _connector().validate({**CFG, "prefix": "reports/"}, _Secrets())
    assert res.ok is True


# ------------------------------------------------------------------ listing

async def test_discover_lists_the_bucket() -> None:
    s3 = _S3([("notes.txt", 10), ("docs/readme.md", 20)])
    _install(s3)
    items = await _connector().discover(CFG, _Secrets())
    assert [i.title for i in items] == ["notes.txt", "docs/readme.md"]
    assert items[0].uri == "s3://my-bucket/notes.txt"


async def test_a_prefix_is_passed_to_s3() -> None:
    s3 = _S3([])
    _install(s3)
    await _connector().discover({**CFG, "prefix": "reports/"}, _Secrets())
    assert s3.requests[0].url.params["prefix"] == "reports/"


async def test_aws_uses_a_virtual_host_and_an_endpoint_override_uses_path_style() -> None:
    s3 = _S3([])
    _install(s3)
    await _connector().discover(CFG, _Secrets())
    assert s3.requests[0].url.host == "my-bucket.s3.eu-central-1.amazonaws.com"

    s3b = _S3([])
    _install(s3b)
    endpoint_auth = _Secrets()
    endpoint_auth.credentials[("test-credential-id", "endpoint")] = "https://minio.internal:9000"
    await _connector().discover(CFG, endpoint_auth)
    assert s3b.requests[0].url.host == "minio.internal"
    assert s3b.requests[0].url.path == "/my-bucket/"


async def test_a_denied_request_says_what_to_check() -> None:
    s3 = _S3([])
    s3.status_override = 403
    _install(s3)
    res = await _connector().validate(CFG, _Secrets())
    assert res.ok is False
    assert "access key" in (res.error or "").lower()


@pytest.mark.parametrize("region", ["x@10.0.0.5:8443/#", "eu-central-1.evil.test#", "EU-1"])
async def test_a_region_that_is_not_a_region_name_never_reaches_the_network(region: str) -> None:
    s3 = _S3([])
    _install(s3)
    auth = _Secrets()
    auth.credentials[("test-credential-id", "region")] = region
    res = await _connector().validate(CFG, auth)
    assert res.ok is False
    assert "region" in (res.error or "")
    assert region not in (res.error or "")
    assert s3.requests == []


@pytest.mark.parametrize(
    "bucket",
    ["x@10.0.0.5:8443/#", "10.0.0.5", "My-Bucket", "ab", "a" * 64, "-bucket", "bucket-", "a_b"],
)
async def test_a_bucket_that_is_not_an_s3_bucket_name_never_reaches_the_network(
    bucket: str,
) -> None:
    s3 = _S3([])
    _install(s3)
    res = await _connector().validate({**CFG, "bucket": bucket}, _Secrets())
    assert res.ok is False
    assert "bucket" in (res.error or "")
    assert bucket not in (res.error or "")
    assert s3.requests == []


def _endpoint_auth() -> _Secrets:
    auth = _Secrets()
    auth.credentials[("test-credential-id", "endpoint")] = "https://ceph.acme.test:7480"
    return auth


async def test_a_custom_endpoint_accepts_a_self_hosted_bucket_name_in_the_path() -> None:
    s3 = _S3([])
    _install(s3)
    res = await _connector().validate({**CFG, "bucket": "My_Bucket"}, _endpoint_auth())
    assert res.ok is True, res.error
    assert s3.requests[0].url.host == "ceph.acme.test"
    assert s3.requests[0].url.raw_path.startswith(b"/My_Bucket/")


@pytest.mark.parametrize(
    "bucket",
    [
        "a/b", "a?b", "a#b", "x@10.0.0.5", "a:b", "a\\b",
        "a b", "a\tb", "a\x00b", ".", "..", "a" * 256,
    ],
)
async def test_a_custom_endpoint_refuses_bucket_names_that_alter_the_url(bucket: str) -> None:
    s3 = _S3([])
    _install(s3)
    res = await _connector().validate({**CFG, "bucket": bucket}, _endpoint_auth())
    assert res.ok is False
    assert "bucket" in (res.error or "")
    assert s3.requests == []


async def test_the_aws_host_still_needs_an_s3_bucket_name() -> None:
    s3 = _S3([])
    _install(s3)
    res = await _connector().validate({**CFG, "bucket": "My_Bucket"}, _Secrets())
    assert res.ok is False
    assert s3.requests == []


async def test_a_dotted_bucket_name_is_fine() -> None:
    s3 = _S3([])
    _install(s3)
    res = await _connector().validate({**CFG, "bucket": "logs.acme-2"}, _Secrets())
    assert res.ok is True, res.error
    assert s3.requests[0].url.host == "logs.acme-2.s3.eu-central-1.amazonaws.com"


async def test_an_s3_error_does_not_repeat_the_upstream_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<Error>internal 10.1.2.3 secret-ish</Error>")

    oauth_http.set_transport_override(httpx.MockTransport(handler))
    res = await _connector().validate(CFG, _Secrets())
    assert res.ok is False
    assert "500" in (res.error or "")
    assert "10.1.2.3" not in (res.error or "")


# ------------------------------------------------------------------ fetching

async def test_fetch_pulls_text_objects() -> None:
    s3 = _S3([("notes.txt", 11)], {"notes.txt": "hello world"})
    _install(s3)
    docs = await _collect(_connector(), CFG, None, _Secrets())
    assert len(docs) == 1
    assert docs[0].content == "hello world"
    assert docs[0].title == "notes.txt"
    assert docs[0].content_type == "text/plain"
    assert docs[0].metadata["key"] == "notes.txt"


async def test_unsupported_binary_objects_are_skipped_and_never_downloaded() -> None:
    s3 = _S3(
        [("photo.jpg", 100), ("ok.md", 4)],
        {"photo.jpg": "\xff\xd8 junk", "ok.md": "# Hi"},
    )
    _install(s3)
    docs = await _collect(_connector(), CFG, None, _Secrets())
    assert [d.title for d in docs] == ["ok.md"]
    assert not [r for r in s3.requests if "photo.jpg" in r.url.path]


async def test_fetch_base64_encodes_pdf_objects() -> None:
    # `extract_text()` (knowledge/ingest.py) requires a PDF as a base64
    # string against `content_type="application/pdf"` -- this pins that
    # exact contract from the connector's side, matching the upload
    # connector's PDF path.
    raw = b"%PDF-1.4 fake pdf body"
    s3 = _S3([("scan.pdf", len(raw))], {"scan.pdf": raw.decode()})
    _install(s3)
    docs = await _collect(_connector(), CFG, None, _Secrets())
    assert len(docs) == 1
    assert docs[0].title == "scan.pdf"
    assert docs[0].content_type == "application/pdf"
    assert base64.b64decode(docs[0].content) == raw


async def test_oversized_objects_are_skipped_by_their_listed_size() -> None:
    s3 = _S3([("huge.txt", 50 * 1024 * 1024)], {"huge.txt": "x"})
    _install(s3)
    assert await _collect(_connector(), CFG, None, _Secrets()) == []


async def test_empty_objects_are_skipped() -> None:
    s3 = _S3([("blank.txt", 3)], {"blank.txt": "  \n"})
    _install(s3)
    assert await _collect(_connector(), CFG, None, _Secrets()) == []


async def test_already_seen_content_is_not_re_emitted() -> None:
    s3 = _S3([("notes.txt", 5)], {"notes.txt": "same"})
    _install(s3)
    c = _connector()
    first = await _collect(c, CFG, None, _Secrets())
    cursor = {"hashes": [first[0].content_hash]}
    assert await _collect(c, CFG, cursor, _Secrets()) == []


async def test_max_files_is_honoured() -> None:
    keys = [(f"f{i}.txt", 5) for i in range(10)]
    s3 = _S3(keys, {f"f{i}.txt": f"body {i}" for i in range(10)})
    _install(s3)
    docs = await _collect(_connector(), {**CFG, "maxFiles": 3}, None, _Secrets())
    assert len(docs) == 3
    assert s3.requests[0].url.params["max-keys"] == "3"


async def test_keys_with_spaces_are_url_encoded() -> None:
    s3 = _S3([("my report.txt", 4)], {"my report.txt": "body"})
    _install(s3)
    docs = await _collect(_connector(), CFG, None, _Secrets())
    assert len(docs) == 1, "a key with a space must still be fetchable"


async def test_only_read_requests_are_ever_issued() -> None:
    s3 = _S3([("a.txt", 4)], {"a.txt": "body"})
    _install(s3)
    c = _connector()
    await c.validate(CFG, _Secrets())
    await c.discover(CFG, _Secrets())
    await _collect(c, CFG, None, _Secrets())
    assert {r.method for r in s3.requests} == {"GET"}


async def test_every_request_is_signed() -> None:
    s3 = _S3([("a.txt", 4)], {"a.txt": "body"})
    _install(s3)
    await _collect(_connector(), CFG, None, _Secrets())
    assert s3.requests
    for r in s3.requests:
        assert r.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "x-amz-date" in r.headers


async def test_it_satisfies_the_connector_protocol() -> None:
    from oc8.knowledge.connectors.base import Connector

    assert isinstance(_connector(), Connector)
    assert _connector().requires_oauth is None


async def test_the_tenant_gate_applies(app_session: Any) -> None:
    import uuid

    from oc8.knowledge.connectors.registry import resolve_connector

    _connector()
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(ConnectorError):
            await resolve_connector(db, tenant_id=tenant, type_id="s3")
