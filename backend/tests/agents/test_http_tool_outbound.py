"""Manual HTTP tools reach only public addresses (or ones the operator
allowed), every redirect hop is checked, the agent gets at most 1 MB, and an
error never carries an auth value. Real loopback server, no mocks."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2
import pytest

from oc8.agent.http_tools import (
    MAX_RESPONSE_BYTES,
    TRUNCATION_MARKER,
    HttpToolError,
    HttpToolSession,
)
from oc8.agent.mcp_client import McpSession, guard_mcp_request
from oc8.net.outbound import OutboundBlocked

pytestmark = pytest.mark.asyncio

_BIG = b"x" * (MAX_RESPONSE_BYTES + 500_000)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/to-metadata":
            self._send(302, b"", {"Location": "http://169.254.169.254/latest/meta-data"})
        elif self.path == "/to-private":
            self._send(302, b"", {"Location": "http://10.0.0.7/admin"})
        elif self.path == "/to-same":
            self._send(302, b"", {"Location": "/ok"})
        elif self.path == "/ok":
            self._send(200, b'{"ok": true}')
        elif self.path == "/big":
            self._send(200, _BIG)
        elif self.path == "/echo-auth":
            # A careless API that reflects the credential it was sent.
            self._send(401, f"bad token {self.headers.get('X-Api-Key')}".encode())
        else:
            self._send(404, b"nope")


@pytest.fixture
def server(allow_loopback_outbound: None):
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _tool(path: str, name: str = "t") -> dict[str, object]:
    return {"name": name, "method": "GET", "url_template": path, "param_schema": {}}


@pytest.mark.parametrize(
    "base_url",
    ["http://169.254.169.254", "http://10.0.0.7", "http://127.0.0.1:9", "http://[::1]:9"],
)
async def test_private_and_metadata_base_urls_are_blocked(base_url: str) -> None:
    async with HttpToolSession(base_url, [_tool("/x")]) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "blocked"
    assert "not allowed" in str(caught.value)


async def test_redirect_to_metadata_address_is_blocked(server: str) -> None:
    async with HttpToolSession(server, [_tool("/to-metadata")]) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "blocked"


async def test_redirect_to_private_address_is_blocked(server: str) -> None:
    async with HttpToolSession(server, [_tool("/to-private")]) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "blocked"


async def test_redirect_within_the_allowed_host_is_followed(server: str) -> None:
    async with HttpToolSession(server, [_tool("/to-same")]) as session:
        assert await session.call("t", {}) == '{"ok": true}'


async def test_body_is_capped_at_one_megabyte_with_a_marker(server: str) -> None:
    async with HttpToolSession(server, [_tool("/big")]) as session:
        out = await session.call("t", {})
        raw = await session.request("t", {})
    assert out.endswith(TRUNCATION_MARKER)
    assert len(out) == MAX_RESPONSE_BYTES + len(TRUNCATION_MARKER)
    assert raw.truncated is True


async def test_error_excerpt_never_contains_header_secret(server: str) -> None:
    async with HttpToolSession(
        server, [_tool("/echo-auth")], headers={"X-Api-Key": "sk-live-SECRET-123"}
    ) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "auth"
    assert caught.value.status == 401
    assert "sk-live-SECRET-123" not in str(caught.value)
    assert "[redacted]" in str(caught.value)


async def test_unreachable_is_its_own_error_class(allow_loopback_outbound: None) -> None:
    # Port 9 (discard) on loopback: allowed by the operator, nobody listening.
    async with HttpToolSession("http://127.0.0.1:9", [_tool("/x")]) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "unreachable"


async def test_remote_mcp_server_url_is_guarded() -> None:
    with pytest.raises(OutboundBlocked):
        async with McpSession("", [], transport="http", server_url="http://169.254.169.254/mcp"):
            pass


async def test_remote_mcp_guard_checks_every_hop() -> None:
    with pytest.raises(OutboundBlocked):
        await guard_mcp_request(httpx2.Request("GET", "http://10.0.0.5/mcp"))
