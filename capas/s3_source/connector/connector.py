"""S3 knowledge connector, shipped as a plugin.

Works against Amazon S3 and any S3-compatible store (MinIO, Ceph, Hetzner,
Wasabi) via an optional `endpoint` override.

**Credentials never live in the source config.** The config carries only the
*id* of a shared `s3_api` credential (access key, secret key, region, and an
optional endpoint override); the values themselves sit in the tenant's
encrypted secret store and are resolved at request time through the
connector's `AuthContext.credential(...)`. So a leaked `data_source` row
leaks a bucket name, not an AWS key -- and the same credential can back any
number of buckets without re-entering the keys.

Scope, stated plainly so nothing surprising lands in a knowledge base:

* **Text and PDF only.** Files whose key ends in a known text extension, or
  `.pdf`, are ingested; everything else is skipped rather than pulled in as
  binary garbage. PDFs are fetched as raw bytes and base64-encoded before
  `extract_text()` parses them (`content_type="application/pdf"`) -- text
  extensions are fetched and stored as plain UTF-8, same as before.
* **Read-only.** Only GET requests are ever signed and issued.
* **Size-capped** per object, so one huge file cannot blow up a sync.
* **It attests what it holds.** `attest_listing` follows every continuation
  token to the end of the bucket and swears the result is complete, which is
  what lets the core conclude that a document missing from it was deleted
  upstream. `maxFiles` bounds what is *ingested*; it never bounds that listing,
  because a listing that stopped early cannot be told apart from objects that
  are gone.

Requests are signed with AWS Signature V4 using stdlib `hmac`/`hashlib` — no SDK
dependency, and the signing is unit-tested against known-answer vectors.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from oc8.knowledge.connectors.base import (
    Attestation,
    AuthContext,
    ConnectorError,
    RawDocument,
    SourceItemMeta,
    SourceListing,
    ValidationResult,
)
from oc8.oauth.http import get_client

_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_MAX_BYTES = 5 * 1024 * 1024
_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

INGESTIBLE_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".log": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".pdf": "application/pdf",
}


def _content_type_for(key: str) -> str | None:
    lowered = key.lower()
    for ext, ctype in INGESTIBLE_EXTENSIONS.items():
        if lowered.endswith(ext):
            return ctype
    return None


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    k_date = _sign(f"AWS4{secret}".encode(), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, _SERVICE)
    return _sign(k_service, "aws4_request")


def sign_request(
    *,
    method: str,
    host: str,
    path: str,
    query: str,
    access_key: str,
    secret_key: str,
    region: str,
    now: dt.datetime,
) -> dict[str, str]:
    """AWS SigV4 headers for a bodyless (GET) S3 request.

    Split out and exported so the signing can be tested against known-answer
    vectors rather than only "it didn't crash".
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{_EMPTY_SHA256}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, path, query, canonical_headers, signed_headers, _EMPTY_SHA256]
    )

    scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Authorization": (
            f"{_ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": _EMPTY_SHA256,
    }


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class _Client:
    """Just enough S3: sign a GET, send it, and complain usefully on failure."""

    def __init__(
        self,
        bucket: str,
        region: str,
        endpoint: str,
        access_key: str,
        secret_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        #: Optional: connect through this transport (a pinned, already-checked
        #: address) instead of resolving the endpoint again.
        self.transport = transport
        self.bucket = bucket
        self.region = region or "us-east-1"
        self.access_key = access_key
        self.secret_key = secret_key
        endpoint = endpoint.strip().rstrip("/")
        if endpoint:
            # S3-compatible store: path-style, bucket in the path.
            self.scheme, _, self.host = endpoint.partition("://")
            if not self.host:
                self.host, self.scheme = self.scheme, "https"
            self.prefix_path = f"/{self.bucket}"
        else:
            self.scheme = "https"
            self.host = f"{self.bucket}.s3.{self.region}.amazonaws.com"
            self.prefix_path = ""

    async def _request(
        self, path: str, query_params: list[tuple[str, str]]
    ) -> httpx.Response:
        full_path = f"{self.prefix_path}{path}" or "/"
        # SigV4 requires the canonical query string sorted by key, with both key
        # and value URI-encoded.
        canonical_query = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(query_params)
        )
        headers = sign_request(
            method="GET",
            host=self.host,
            path=full_path,
            query=canonical_query,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            now=_now(),
        )
        url = f"{self.scheme}://{self.host}{full_path}"
        if canonical_query:
            url = f"{url}?{canonical_query}"
        async with get_client(transport=self.transport) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 403:
            raise ConnectorError(
                "S3 denied the request — check the access key, its permissions, and the region"
            )
        if resp.status_code == 404:
            raise ConnectorError(f"S3 bucket or object not found: {self.bucket}")
        if resp.status_code != 200:
            raise ConnectorError(f"S3 error (HTTP {resp.status_code}): {resp.text[:200]}")
        if len(resp.content) > _MAX_BYTES:
            raise ConnectorError("S3 object exceeds the 5 MB limit")
        return resp

    async def get(self, path: str, query_params: list[tuple[str, str]]) -> str:
        """A text response -- the object listing XML, or a text/markdown/etc
        object body. Decoded as UTF-8; use `get_bytes` for binary content
        (PDF) instead, or this mangles it."""
        resp = await self._request(path, query_params)
        return resp.text

    async def get_bytes(self, path: str, query_params: list[tuple[str, str]]) -> bytes:
        resp = await self._request(path, query_params)
        return resp.content


async def _credentials(config: dict[str, Any], auth: AuthContext | None) -> tuple[str, str]:
    if auth is None:
        raise ConnectorError("no credential context available for this source")
    credential_id = str(config.get("credential", "")).strip()
    if not credential_id:
        raise ConnectorError("credential must be set")
    return (
        await auth.credential(credential_id, "access_key"),
        await auth.credential(credential_id, "secret_key"),
    )


async def _client_for(config: dict[str, Any], auth: AuthContext | None) -> _Client:
    """Build the `_Client` for this DataSource's config, resolving
    access_key/secret_key/region/endpoint from the credential -- none of
    the four ever comes from the DataSource's own config any more. `bucket`
    stays on the DataSource, since it names WHAT to sync, not WHO can."""
    access_key, secret_key = await _credentials(config, auth)
    # `_credentials` already raised if `auth` were None or `credential`
    # unset, so both are safe to use again here.
    assert auth is not None
    credential_id = str(config.get("credential", "")).strip()
    region = await auth.credential(credential_id, "region")
    endpoint = await auth.credential(credential_id, "endpoint")
    bucket = str(config.get("bucket", ""))
    return _Client(bucket, region, endpoint, access_key, secret_key)


def _object_uri(bucket: str, key: str) -> str:
    """The one place an object's URI is built.

    `attest_listing` and `fetch` must produce byte-identical strings. The trap
    is specific and cheap to fall into: a listing of bare keys against a corpus
    of `s3://bucket/key` shares zero strings with it, so every stored document
    reads as absent from every listing and the source is wiped.
    """
    return f"s3://{bucket}/{key}"


def _parse_page(xml: str) -> tuple[list[tuple[str, int]], str]:
    """(key, size) pairs from one ListObjectsV2 response, plus the token for the
    next page -- empty when this was the last one."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ConnectorError(f"S3 returned a response we could not parse: {exc}") from None
    out: list[tuple[str, int]] = []
    for contents in root.findall("s3:Contents", _NS) or root.findall("Contents"):
        key_el = contents.find("s3:Key", _NS)
        if key_el is None:
            key_el = contents.find("Key")
        size_el = contents.find("s3:Size", _NS)
        if size_el is None:
            size_el = contents.find("Size")
        if key_el is None or not key_el.text:
            continue
        size = int(size_el.text) if size_el is not None and size_el.text else 0
        out.append((key_el.text, size))
    # S3 sends NextContinuationToken only when IsTruncated is true, so its
    # presence is the whole signal; reading IsTruncated as well would add a
    # second thing that can disagree with the first.
    token_el = root.find("s3:NextContinuationToken", _NS)
    if token_el is None:
        token_el = root.find("NextContinuationToken")
    token = (token_el.text or "") if token_el is not None else ""
    return out, token


def _parse_keys(xml: str) -> list[tuple[str, int]]:
    """(key, size) pairs from a ListObjectsV2 response."""
    return _parse_page(xml)[0]


class S3Connector:
    type_id = "s3"
    label = "S3 / object storage"
    description = "Text files from an S3-compatible bucket"
    requires_oauth: str | None = None
    config_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "bucket": {"type": "string", "title": "Bucket"},
            "prefix": {
                "type": "string",
                "title": "Prefix",
                "default": "",
                "description": "Only objects whose key starts with this are included. "
                "Leave empty to scan the whole bucket -- do not put the bucket name "
                "here.",
            },
            "credential": {
                "type": "string",
                "title": "S3 credentials",
                "credentialType": "s3_api",
            },
            "maxFiles": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
        },
        "required": ["bucket", "credential"],
    }

    async def _list(
        self, config: dict[str, Any], auth: AuthContext | None
    ) -> tuple[_Client, list[tuple[str, int]]]:
        client = await _client_for(config, auth)
        max_files = int(config.get("maxFiles", 100))
        params = [("list-type", "2"), ("max-keys", str(max_files))]
        prefix = str(config.get("prefix", "")).strip()
        if prefix:
            params.append(("prefix", prefix))
        xml = await client.get("/", params)
        return client, _parse_keys(xml)[:max_files]

    async def _list_every_key(
        self, config: dict[str, Any], auth: AuthContext | None
    ) -> tuple[_Client, list[str]]:
        """The bucket (under its prefix), all of it, however many pages it takes.

        Deliberately not `_list`: that one stops at `maxFiles` because what it
        feeds gets embedded and stored. A listing that stops early cannot be
        told apart from the objects past the stop having been deleted, so a
        capped listing would silently condemn the tail of any bucket bigger than
        `maxFiles`. Keys and sizes are cheap -- one request per 1000 objects.
        """
        client = await _client_for(config, auth)
        prefix = str(config.get("prefix", "")).strip()
        keys: list[str] = []
        token = ""
        while True:
            params = [("list-type", "2"), ("max-keys", "1000")]
            if prefix:
                params.append(("prefix", prefix))
            if token:
                params.append(("continuation-token", token))
            page, token = _parse_page(await client.get("/", params))
            keys.extend(key for key, _size in page)
            if not token:
                return client, keys

    async def validate(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> ValidationResult:
        bucket = str(config.get("bucket", "")).strip()
        if not bucket:
            return ValidationResult(ok=False, error="bucket is required")
        # Live, 2026-08-21: a user typed the bucket name into prefix too --
        # S3 prefix-matches against the KEY, not a folder named after the
        # bucket, so that source listed 0 objects forever with no error at
        # all, identical to an honestly-empty bucket. A prefix equal to its
        # own bucket name is a near-certain copy/paste, never a real prefix
        # (nothing is ever nested a folder deep named after its own bucket),
        # so this is precise enough to refuse outright rather than warn.
        prefix = str(config.get("prefix", "")).strip()
        if prefix and prefix == bucket:
            return ValidationResult(
                ok=False,
                error=(
                    f"prefix {prefix!r} is the same as the bucket name -- objects are "
                    "matched by their key, not nested under a folder named after the "
                    "bucket. Leave prefix empty to scan the whole bucket, or use the "
                    "folder path inside it instead."
                ),
            )
        max_files = config.get("maxFiles", 100)
        if not isinstance(max_files, int) or not 1 <= max_files <= 1000:
            return ValidationResult(ok=False, error="maxFiles must be between 1 and 1000")
        try:
            await self._list(config, auth)
        except ConnectorError as exc:
            return ValidationResult(ok=False, error=str(exc))
        return ValidationResult(ok=True)

    async def discover(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> list[SourceItemMeta]:
        client, keys = await self._list(config, auth)
        return [
            SourceItemMeta(uri=_object_uri(client.bucket, key), title=key) for key, _size in keys
        ]

    async def attest_listing(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> SourceListing:
        """Every object under the configured prefix -- the whole bucket, or nothing.

        The reason this was once deferred was that attesting over a truncated
        `ListObjectsV2` needs honest continuation-token handling; that handling
        is now the requirement rather than the obstacle (`_list_every_key`).

        A denied or failed request raises `ConnectorError` and attests nothing:
        an expired key returning AUTHORITATIVE with no URIs is the wire shape of
        a customer who emptied their bucket.
        """
        client, keys = await self._list_every_key(config, auth)
        return SourceListing(
            attestation=Attestation.AUTHORITATIVE,
            present_uris=frozenset(_object_uri(client.bucket, key) for key in keys),
        )

    async def fetch(
        self,
        config: dict[str, Any],
        cursor: dict[str, Any] | None,
        auth: AuthContext | None = None,
    ) -> AsyncIterator[RawDocument]:
        seen = set((cursor or {}).get("hashes", []))
        client, keys = await self._list(config, auth)
        for key, size in keys:
            content_type = _content_type_for(key)
            if content_type is None:
                continue  # not an ingestible type -- skipped, never ingested as bytes
            if size > _MAX_BYTES:
                continue
            object_path = "/" + quote(key, safe="/")
            if content_type == "application/pdf":
                # Raw bytes, never decoded as text -- `extract_text()` expects
                # a PDF as a base64 string (`content_type="application/pdf"`),
                # the same contract the upload connector's PDF path uses.
                raw_bytes = await client.get_bytes(object_path, [])
                body = base64.b64encode(raw_bytes).decode("ascii")
            else:
                body = await client.get(object_path, [])
            if not body.strip():
                continue
            digest = hashlib.sha256(body.encode()).hexdigest()
            if digest in seen:
                continue
            yield RawDocument(
                source_uri=_object_uri(client.bucket, key),
                title=key.rsplit("/", 1)[-1],
                content=body,
                content_type=content_type,
                content_hash=digest,
                metadata={"bucket": client.bucket, "key": key, "size": str(size)},
            )


def register(contrib: Any) -> None:
    contrib.add_connector(S3Connector())
