"""validate_odoo_login is the odoo_login credential type's real
validate_entry_point (§ credential.py's own docstring): a live-observed bug
had it entirely unwired, so the Credentials page's "Test" button reported
success unconditionally without ever asking Odoo. A second live-observed
bug had it call `/web/session/authenticate`, which Odoo refuses for an API
key by design -- so it reported "Access Denied" for credentials that work
fine in real use. These tests exercise the function directly against a
fake httpx transport -- no real Odoo needed."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    for _stale in [n for n in sys.modules if n == "credential" or n.startswith("credential.")]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from credential import validate_odoo_login  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio

_VALUES = {
    "url": "https://odoo.acme.test",
    "database": "oc8",
    "username": "admin",
    "password": "sk-odoo-test",
}


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


def _patch_post(monkeypatch: pytest.MonkeyPatch, json_body: dict[str, object]) -> None:
    async def fake_post(
        self: httpx.AsyncClient, url: str, json: dict[str, object] | None = None, **kw: object
    ) -> httpx.Response:
        return httpx.Response(200, json=json_body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def test_a_truthy_uid_result_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, {"jsonrpc": "2.0", "id": None, "result": 2})
    await validate_odoo_login(_VALUES)  # must not raise


async def test_an_access_denied_error_body_is_rejected_with_the_real_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON-RPC `error` envelope (rare for common.authenticate, but the
    transport can still surface one, e.g. a malformed db name)."""
    _patch_post(
        monkeypatch,
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": 0,
                "message": "Odoo Server Error",
                "data": {"name": "odoo.exceptions.AccessDenied", "message": "Access Denied"},
            },
        },
    )
    with pytest.raises(ValueError, match="Access Denied"):
        await validate_odoo_login(_VALUES)


async def test_a_false_result_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live-observed shape for bad credentials: HTTP 200, no error
    envelope, `result` is the bare literal `false` -- common.authenticate's
    documented failure return, not an exception."""
    _patch_post(monkeypatch, {"jsonrpc": "2.0", "id": None, "result": False})
    with pytest.raises(ValueError, match="invalid username, password, or API key"):
        await validate_odoo_login(_VALUES)


async def test_unreachable_host_raises_a_clean_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(self: httpx.AsyncClient, *a: object, **kw: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(ValueError, match=r"^could not reach the Odoo server$"):
        await validate_odoo_login(_VALUES)


@pytest.mark.parametrize("missing", ["url", "database", "username", "password"])
async def test_each_required_field_is_checked_before_any_network_call(missing: str) -> None:
    values = dict(_VALUES)
    values[missing] = ""
    with pytest.raises(ValueError, match=missing):
        await validate_odoo_login(values)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8069",
        "http://10.0.0.5",
        "http://169.254.169.254",
        "http://[::1]",
        "ftp://x.test",
    ],
)
async def test_internal_addresses_are_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    async def boom(self: httpx.AsyncClient, *a: object, **kw: object) -> httpx.Response:
        raise AssertionError("no request may be made")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(ValueError, match="not reachable"):
        await validate_odoo_login({**_VALUES, "url": url})


async def test_a_name_resolving_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["93.184.216.34", "10.1.1.1"])
    with pytest.raises(ValueError, match="not reachable"):
        await validate_odoo_login(_VALUES)


async def test_the_request_is_pinned_to_the_checked_address_and_follows_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oc8.net.outbound import PinnedTransport

    seen: dict[str, object] = {}
    _patch_post(monkeypatch, {"jsonrpc": "2.0", "id": None, "result": 2})
    real = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        seen.update(kwargs)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    await validate_odoo_login(_VALUES)
    assert seen["follow_redirects"] is False
    transport = seen["transport"]
    assert isinstance(transport, PinnedTransport)
    assert transport._ip == "93.184.216.34"


async def test_a_redirect_answer_is_an_error_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(self: httpx.AsyncClient, url: str, **kw: object) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(ValueError, match=r"^Odoo answered with HTTP 302$"):
        await validate_odoo_login(_VALUES)


async def test_http_and_parse_errors_are_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    async def server_error(self: httpx.AsyncClient, url: str, **kw: object) -> httpx.Response:
        return httpx.Response(
            500, text="secret sk-live-1 http://10.0.0.9", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", server_error)
    with pytest.raises(ValueError) as err:
        await validate_odoo_login(_VALUES)
    assert str(err.value) == "Odoo answered with HTTP 500"

    async def not_json(self: httpx.AsyncClient, url: str, **kw: object) -> httpx.Response:
        return httpx.Response(
            200, text="<html>sk-live-1 http://10.0.0.9</html>", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", not_json)
    with pytest.raises(ValueError) as err2:
        await validate_odoo_login(_VALUES)
    assert "sk-live-1" not in str(err2.value) and "10.0.0.9" not in str(err2.value)


async def test_an_upstream_error_message_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(
        monkeypatch,
        {
            "error": {
                "message": "boom",
                "data": {"message": "db at http://10.0.0.9 token sk-live-1"},
            }
        },
    )
    with pytest.raises(ValueError) as err:
        await validate_odoo_login(_VALUES)
    assert "sk-live-1" not in str(err.value) and "10.0.0.9" not in str(err.value)
