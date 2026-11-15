"""The Odoo client behind the knowledge connector, against a fake Odoo.

The fake answers `/jsonrpc` (`common.version`, `common.authenticate`,
`object.execute_kw`) and, when it plays Odoo 19, the JSON-2 API
(`/json/2/<model>/<method>`). It is injected through `open_client`'s
`transport=`; the outbound guard still runs, so names resolve through the
patched resolver below and an internal address is refused before any request.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """`connector` is a package name several capas share, and sys.modules is
    keyed by name: drop any cached copy before and after the import below."""
    for _stale in [n for n in sys.modules if n == "connector" or n.startswith("connector.")]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from connector.odoo_client import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    OdooAccessDenied,
    OdooAppMissing,
    OdooError,
    OdooLoginRefused,
    OdooUnreachable,
    open_client,
)

sys.path.remove(str(PLUGIN_ROOT))
_evict()

from oc8.knowledge.connectors.base import ConnectorError  # noqa: E402

pytestmark = pytest.mark.asyncio

URL = "https://odoo.acme.test"
HOST = "odoo.acme.test"
DB = "acme-prod-db"
LOGIN = "knowledge-bot@acme.test"
SECRET = "sk-odoo-0123456789abcdef"
UID = 7


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names resolve to a public address; IP literals and localhost stay put."""
    import ipaddress

    def resolve(host: str) -> list[str]:
        if host == "localhost":
            return ["127.0.0.1"]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return ["93.184.216.34"]
        return [host]

    monkeypatch.setattr("oc8.net.outbound.resolve_ips", resolve)


class _Streamed(httpx.AsyncByteStream):
    """A body that arrives as a stream, as from the network. httpx reads a
    MockTransport response's body up front, which a bounded reader (one that
    reads the raw stream itself) could not read again."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> Any:
        yield self._data


def _mock(handler: Any) -> httpx.MockTransport:
    def streamed(request: httpx.Request) -> httpx.Response:
        response: httpx.Response = handler(request)
        headers = [(k, v) for k, v in response.headers.items() if k.lower() != "content-length"]
        return httpx.Response(
            response.status_code, headers=headers, stream=_Streamed(response.content)
        )

    return httpx.MockTransport(streamed)


def _rpc_error(name: str, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": 200,
            "message": "Odoo Server Error",
            "data": {"name": name, "message": message, "debug": f"Traceback ... {DB} {LOGIN}"},
        },
    }


class FakeOdoo:
    """Just enough Odoo. `json2_probe` is the status JSON-2 answers with
    (None: JSON-2 works); `fail` makes every model call fail that way."""

    def __init__(
        self,
        version: list[Any],
        *,
        json2_probe: int | None = None,
        authenticate: Any = UID,
        fail: str | None = None,
    ) -> None:
        self.version = version
        self.json2_probe = json2_probe
        self.authenticate = authenticate
        self.fail = fail
        self.requests: list[httpx.Request] = []
        self.models = {"res.partner": [{"id": 1, "name": "Acme"}]}

    def transport(self) -> httpx.MockTransport:
        return _mock(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path == "/jsonrpc":
            return self._jsonrpc(body)
        if path.startswith("/json/2/"):
            if self.version[0] < 19:
                return httpx.Response(404, text="Not Found")
            if self.json2_probe is not None:
                return httpx.Response(self.json2_probe, json={"name": "x", "message": "no"})
            _, _, _, model, method = path.split("/")
            return self._json2(model, method, body)
        return httpx.Response(404)

    def _jsonrpc(self, body: dict[str, Any]) -> httpx.Response:
        params = body["params"]
        service, method, args = params["service"], params["method"], params["args"]
        if service == "common" and method == "version":
            return self._ok({"server_version": "x", "server_version_info": self.version})
        if service == "common" and method == "authenticate":
            return self._ok(self.authenticate)
        assert service == "object" and method == "execute_kw"
        _db, _uid, _pw, model, meth, pos, kw = args
        if self.fail == "access":
            return httpx.Response(200, json=_rpc_error("odoo.exceptions.AccessError", "no"))
        if self.fail == "server":
            return httpx.Response(502, text="Bad Gateway")
        if model not in self.models and model != "res.users":
            return httpx.Response(
                200,
                json=_rpc_error("odoo.exceptions.UserError", f"Object {model} doesn't exist"),
            )
        return self._ok({"model": model, "method": meth, "args": pos, "kwargs": kw})

    def _json2(self, model: str, method: str, body: dict[str, Any]) -> httpx.Response:
        if self.fail == "access" and model != "res.users":
            return httpx.Response(
                403, json={"name": "odoo.exceptions.AccessError", "message": f"no {LOGIN}"}
            )
        if self.fail == "server":
            return httpx.Response(500, json={"name": "builtins.Exception", "message": DB})
        if model not in self.models and model != "res.users":
            return httpx.Response(
                404, json={"name": "werkzeug.exceptions.NotFound", "message": model}
            )
        return httpx.Response(200, json={"model": model, "method": method, "kwargs": body})

    @staticmethod
    def _ok(result: Any) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "result": result})


async def _open(fake: FakeOdoo, url: str = URL) -> Any:
    return await open_client(
        url=url, database=DB, login=LOGIN, secret=SECRET, transport=fake.transport()
    )


def _assert_clean(text: str) -> None:
    for value in (HOST, URL, DB, LOGIN, SECRET):
        assert value not in text, f"{value!r} leaked into {text!r}"


def _assert_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        _assert_clean(record.getMessage())
        if record.exc_text:
            _assert_clean(record.exc_text)


async def test_errors_derive_from_connector_error() -> None:
    for cls in (OdooLoginRefused, OdooAccessDenied, OdooAppMissing, OdooUnreachable):
        assert issubclass(cls, OdooError)
    assert issubclass(OdooError, ConnectorError)


async def test_json2_selected_on_odoo_19() -> None:
    fake = FakeOdoo([19, 0, 0, "final", 0, ""])
    client = await _open(fake)
    try:
        assert client.protocol == "json2"
        assert client.version == (19, 0)
        assert client.hint is None
        result = await client.call(
            "res.partner", "search_read", domain=[["id", ">", 0]], fields=["name"], limit=5
        )
    finally:
        await client.aclose()
    assert result["kwargs"] == {"domain": [["id", ">", 0]], "fields": ["name"], "limit": 5}
    last = fake.requests[-1]
    assert last.url.path == "/json/2/res.partner/search_read"
    assert last.headers["authorization"] == f"bearer {SECRET}"
    assert last.headers["x-odoo-database"] == DB
    # The pinned connection keeps the real host name for TLS and the Host header.
    assert last.headers["host"] == HOST
    # JSON-2 needs no /jsonrpc login.
    assert not any(b"authenticate" in r.content for r in fake.requests)


async def test_jsonrpc_selected_on_odoo_18() -> None:
    fake = FakeOdoo([18, 0, 0, "final", 0, ""])
    client = await _open(fake)
    try:
        assert client.protocol == "jsonrpc"
        assert client.version == (18, 0)
        assert client.hint is None
        result = await client.call(
            "res.partner", "search_read", domain=[["id", ">", 0]], fields=["name"]
        )
        read = await client.call("res.partner", "read", ids=[1, 2], fields=["name"])
        fields = await client.call("res.partner", "fields_get", attributes=["type"])
    finally:
        await client.aclose()
    assert result["args"] == [[["id", ">", 0]]]
    assert result["kwargs"] == {"fields": ["name"]}
    assert read["args"] == [[1, 2]]
    assert read["kwargs"] == {"fields": ["name"]}
    assert fields["args"] == []
    assert fields["kwargs"] == {"attributes": ["type"]}
    assert not any(r.url.path.startswith("/json/2/") for r in fake.requests)
    execute = json.loads(fake.requests[-1].content)["params"]["args"]
    assert execute[:3] == [DB, UID, SECRET]


async def test_json2_401_falls_back_to_jsonrpc_with_hint() -> None:
    fake = FakeOdoo([19, 0, 0, "final", 0, ""], json2_probe=401)
    client = await _open(fake)
    try:
        assert client.protocol == "jsonrpc"
        assert client.hint == "api_key_recommended"
        await client.call("res.partner", "search_count", domain=[])
    finally:
        await client.aclose()
    assert fake.requests[-1].url.path == "/jsonrpc"


async def test_json2_404_falls_back_without_hint() -> None:
    fake = FakeOdoo([19, 0, 0, "final", 0, ""], json2_probe=404)
    client = await _open(fake)
    try:
        assert client.protocol == "jsonrpc"
        assert client.hint is None
    finally:
        await client.aclose()


async def test_online_release_version_is_parsed() -> None:
    fake = FakeOdoo(["saas~18", 3, 0, "final", 0, ""])
    client = await _open(fake)
    try:
        assert client.version == (18, 3)
        assert client.protocol == "jsonrpc"
    finally:
        await client.aclose()


async def test_login_refused(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeOdoo([17, 0, 0, "final", 0, ""], authenticate=False)
    with pytest.raises(OdooLoginRefused) as info:
        await _open(fake)
    _assert_clean(str(info.value))
    _assert_logs_clean(caplog)


@pytest.mark.parametrize("version", [[18, 0, 0, "final", 0, ""], [19, 0, 0, "final", 0, ""]])
async def test_access_error_maps_to_access_denied(
    version: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeOdoo(version, fail="access")
    client = await _open(fake)
    try:
        with pytest.raises(OdooAccessDenied) as info:
            await client.call("res.partner", "search", domain=[])
        assert await client.can_read("res.partner") is False
    finally:
        await client.aclose()
    _assert_clean(str(info.value))
    _assert_logs_clean(caplog)


@pytest.mark.parametrize("version", [[18, 0, 0, "final", 0, ""], [19, 0, 0, "final", 0, ""]])
async def test_missing_model_maps_to_app_missing(
    version: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeOdoo(version)
    client = await _open(fake)
    try:
        with pytest.raises(OdooAppMissing) as info:
            await client.call("knowledge.article", "search_read", domain=[], fields=["name"])
        assert await client.can_read("res.partner") is True
    finally:
        await client.aclose()
    _assert_clean(str(info.value))
    _assert_logs_clean(caplog)


async def test_model_exists_true_and_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)["params"]
        if params["method"] == "version":
            result: Any = {"server_version_info": [17, 0, 0, "final", 0, ""]}
        elif params["method"] == "authenticate":
            result = UID
        else:
            assert params["args"][3:5] == ["ir.model", "search_count"]
            domain = params["args"][5][0]
            result = 1 if domain[0][2] == "knowledge.article" else 0
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "result": result})

    client = await open_client(
        url=URL, database=DB, login=LOGIN, secret=SECRET, transport=_mock(handler)
    )
    try:
        assert await client.model_exists("knowledge.article") is True
        assert await client.model_exists("documents.document") is False
    finally:
        await client.aclose()


@pytest.mark.parametrize("version", [[18, 0, 0, "final", 0, ""], [19, 0, 0, "final", 0, ""]])
async def test_server_error_maps_to_unreachable(
    version: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeOdoo(version)
    client = await _open(fake)
    fake.fail = "server"
    try:
        with pytest.raises(OdooUnreachable) as info:
            await client.call("res.partner", "search", domain=[])
    finally:
        await client.aclose()
    _assert_clean(str(info.value))
    _assert_logs_clean(caplog)


async def test_timeout_maps_to_unreachable(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeOdoo([18, 0, 0, "final", 0, ""], fail="timeout")
    with pytest.raises(OdooUnreachable) as info:
        await _open(fake)
    _assert_clean(str(info.value))
    assert info.value.__cause__ is None
    _assert_logs_clean(caplog)


async def test_oversized_answer_is_refused() -> None:
    big = {"jsonrpc": "2.0", "id": None, "result": "x" * (MAX_RESPONSE_BYTES + 10)}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=big)

    with pytest.raises(OdooError, match="too large") as info:
        await open_client(
            url=URL, database=DB, login=LOGIN, secret=SECRET, transport=_mock(handler)
        )
    _assert_clean(str(info.value))


async def test_errors_never_contain_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """Every failure path, one after another: neither the message, the
    chained cause, nor any log line names the host, db, login or secret."""
    caplog.set_level(logging.DEBUG)
    scenarios: list[tuple[FakeOdoo, str | None]] = [
        (FakeOdoo([17, 0, 0, "final", 0, ""], authenticate=False), None),
        (FakeOdoo([18, 0, 0, "final", 0, ""], fail="timeout"), None),
        (FakeOdoo([18, 0, 0, "final", 0, ""]), "access"),
        (FakeOdoo([19, 0, 0, "final", 0, ""]), "access"),
        (FakeOdoo([18, 0, 0, "final", 0, ""]), "server"),
        (FakeOdoo([19, 0, 0, "final", 0, ""]), "server"),
        (FakeOdoo([18, 0, 0, "final", 0, ""]), "missing"),
        (FakeOdoo([19, 0, 0, "final", 0, ""]), "missing"),
    ]
    seen = 0
    for fake, late_fail in scenarios:
        try:
            client = await _open(fake)
            try:
                if late_fail in ("access", "server"):
                    fake.fail = late_fail
                model = "nope.model" if late_fail == "missing" else "res.partner"
                await client.call(model, "search", domain=[])
            finally:
                await client.aclose()
        except OdooError as exc:
            seen += 1
            _assert_clean(str(exc))
            _assert_clean(repr(exc))
            cause = exc.__cause__ or exc.__context__
            while cause is not None:
                _assert_clean(str(cause))
                cause = cause.__cause__ or cause.__context__
    assert seen == len(scenarios)
    _assert_logs_clean(caplog)


async def test_private_address_refused() -> None:
    fake = FakeOdoo([19, 0, 0, "final", 0, ""])
    with pytest.raises(OdooUnreachable) as info:
        await _open(fake, url="http://127.0.0.1")
    assert fake.requests == []
    assert "127.0.0.1" not in str(info.value)


async def test_login_in_url_refused() -> None:
    fake = FakeOdoo([19, 0, 0, "final", 0, ""])
    with pytest.raises(OdooError) as info:
        await _open(fake, url=f"https://{LOGIN.split('@')[0]}:{SECRET}@{HOST}")
    assert fake.requests == []
    _assert_clean(str(info.value))


async def test_record_url_by_version() -> None:
    expected = {
        17: (
            f"{URL}/web#model=documents.document&id=5&view_type=form",
            f"{URL}/web#model=knowledge.article&id=9&view_type=form",
        ),
        18: (f"{URL}/odoo/documents/5", f"{URL}/odoo/knowledge/9"),
        19: (f"{URL}/odoo/documents/5", f"{URL}/odoo/knowledge/9"),
    }
    for major, (documents, knowledge) in expected.items():
        client = await _open(FakeOdoo([major, 0, 0, "final", 0, ""]), url=f"{URL}/")
        try:
            assert client.record_url("documents", 5) == documents
            assert client.record_url("knowledge", 9) == knowledge
        finally:
            await client.aclose()
