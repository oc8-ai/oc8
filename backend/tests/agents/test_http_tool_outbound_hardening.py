"""The edges of the outbound guard for HTTP tools and remote MCP servers: a
secret is redacted wherever it sits in a body and whatever scheme prefix its
header carries, a compressed body cannot blow past the 1 MB cap in memory, a
call has one overall deadline, a refusal never tells the tenant which
internal address a name resolves to, and a remote MCP server is reached at
the address that was checked, on every hop. Real loopback servers, no mocks
of the code under test."""

from __future__ import annotations

import gzip
import threading
import time
import tracemalloc
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oc8.agent.http_tools import (
    ERROR_EXCERPT_CHARS,
    MAX_RESPONSE_BYTES,
    HttpToolError,
    HttpToolSession,
)
from oc8.agent.mcp_client import McpSession
from oc8.net import outbound
from oc8.net.outbound import OutboundBlocked

pytestmark = pytest.mark.asyncio

_KEY = "sk-live-SECRET-123"
_TOKEN = "tok-SECRET-999"
# 64 MB of zeros, about 64 KB on the wire.
_BOMB = gzip.compress(b"\0" * 64_000_000, compresslevel=9)


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, str]]

    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> None:
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})

    def do_GET(self) -> None:
        self._record()
        path = self.path.split("?")[0]
        if path == "/echo-long":
            # The key straddles the excerpt cut.
            body = "a" * (ERROR_EXCERPT_CHARS - 10) + f" key={self.headers.get('X-Api-Key')} end"
            self._send(400, body.encode())
        elif path == "/echo-bearer":
            token = str(self.headers.get("Authorization", "")).removeprefix("Bearer ")
            self._send(401, f"invalid token {token}".encode())
        elif path == "/whoami":
            self._send(200, f'{{"key": "{self.headers.get("X-Api-Key")}"}}'.encode())
        elif path == "/bomb":
            self._send(200, _BOMB, {"Content-Encoding": "gzip"})
        elif path == "/gzip-small":
            self._send(200, gzip.compress(b'{"ok": true}'), {"Content-Encoding": "gzip"})
        elif path == "/brotli":
            self._send(200, b"\x0b\x02\x80ok\x03", {"Content-Encoding": "br"})
        elif path == "/trickle":
            self.send_response(200)
            self.send_header("Content-Length", "20")
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.3)
            except OSError:
                return
        else:
            self._send(404, b"nope")

    def do_POST(self) -> None:
        self._record()
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        target = type(self).redirect_to(self.path)
        if target:
            self._send(307, b"", {"Location": target})
        else:
            self._send(404, b"nope")

    @staticmethod
    def redirect_to(_path: str) -> str:
        return ""


def _serve(handler: type[_Handler]) -> Iterator[tuple[str, int]]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd.server_address[1]
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def server(allow_loopback_outbound: None) -> Iterator[str]:
    handler = type("H", (_Handler,), {"seen": []})
    for url, _port in _serve(handler):
        yield url


def _tool(path: str, name: str = "t") -> dict[str, object]:
    return {"name": name, "method": "GET", "url_template": path, "param_schema": {}}


# --- redaction -------------------------------------------------------------


async def test_a_secret_across_the_excerpt_cut_is_redacted_whole(server: str) -> None:
    async with HttpToolSession(
        server, [_tool("/echo-long")], headers={"X-Api-Key": _KEY}
    ) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    text = str(caught.value)
    # The marker itself may be cut; the secret's first characters must not
    # be there either way.
    assert "sk-" not in text
    assert "key=[re" in text


async def test_a_bearer_token_is_redacted_without_its_scheme(server: str) -> None:
    async with HttpToolSession(
        server, [_tool("/echo-bearer")], headers={"Authorization": f"Bearer {_TOKEN}"}
    ) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "auth"
    assert _TOKEN not in str(caught.value)
    assert _TOKEN not in caught.value.excerpt


async def test_an_explicit_secret_is_redacted(server: str) -> None:
    """A login hands its raw secret parts in (a password, a token) so they
    are redacted even when no header carries them verbatim."""
    async with HttpToolSession(
        server,
        [_tool("/echo-bearer")],
        headers={"Authorization": f"Bearer {_TOKEN}"},
        secrets=["SECRET-999"],
    ) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert "SECRET-999" not in str(caught.value)


async def test_a_successful_body_is_redacted_before_the_agent_sees_it(server: str) -> None:
    async with HttpToolSession(server, [_tool("/whoami")], headers={"X-Api-Key": _KEY}) as session:
        out = await session.call("t", {})
    assert _KEY not in out
    assert "[redacted]" in out


# --- compressed bodies ------------------------------------------------------


async def test_a_gzip_bomb_is_cut_at_the_cap_without_inflating_it(server: str) -> None:
    async with HttpToolSession(server, [_tool("/bomb")]) as session:
        tracemalloc.start()
        try:
            raw = await session.request("t", {})
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert raw.truncated is True
    assert len(raw.text) == MAX_RESPONSE_BYTES
    assert peak < 8_000_000, f"peak {peak} bytes"


async def test_a_small_gzip_body_is_decoded(server: str) -> None:
    async with HttpToolSession(server, [_tool("/gzip-small")]) as session:
        assert await session.call("t", {}) == '{"ok": true}'


async def test_an_unsupported_encoding_is_refused(server: str) -> None:
    async with HttpToolSession(server, [_tool("/brotli")]) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "http"
    assert "encoding" in str(caught.value)


# --- one overall deadline ---------------------------------------------------


async def test_a_trickling_server_hits_the_overall_deadline(server: str) -> None:
    started = time.monotonic()
    async with HttpToolSession(server, [_tool("/trickle")], timeout_s=1.0) as session:
        with pytest.raises(HttpToolError) as caught:
            await session.call("t", {})
    assert caught.value.kind == "unreachable"
    assert time.monotonic() - started < 3.0


# --- refusals do not reveal internal addresses ------------------------------


@pytest.fixture
def internal_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    real = outbound.resolve_ips

    def fake(host: str) -> list[str]:
        if host == "db.internal.test":
            return ["10.43.2.17"]
        if host == "missing.internal.test":
            raise OSError("Name or service not known")
        return real(host)

    monkeypatch.setattr(outbound, "resolve_ips", fake)


@pytest.mark.usefixtures("internal_dns")
async def test_a_refusal_does_not_reveal_the_resolved_address() -> None:
    messages = []
    for host in ("db.internal.test", "missing.internal.test"):
        async with HttpToolSession(f"http://{host}", [_tool("/x")]) as session:
            with pytest.raises(HttpToolError) as caught:
                await session.call("t", {})
        assert caught.value.kind == "blocked"
        messages.append(str(caught.value))
    assert "10.43.2.17" not in messages[0]
    # Whether the name exists or not reads the same.
    assert messages[0] == messages[1] == "This address is not allowed."


@pytest.mark.usefixtures("internal_dns")
async def test_a_remote_mcp_refusal_does_not_reveal_the_resolved_address() -> None:
    with pytest.raises(OutboundBlocked) as caught:
        async with McpSession("", [], transport="http", server_url="http://db.internal.test/mcp"):
            pass
    assert "10.43.2.17" not in str(caught.value)
    assert str(caught.value) == "This address is not allowed."


# --- remote MCP: every hop checked and pinned -------------------------------


async def test_a_remote_mcp_redirect_to_a_private_address_is_blocked(
    allow_loopback_outbound: None,
) -> None:
    handler = type(
        "H",
        (_Handler,),
        {"seen": [], "redirect_to": staticmethod(lambda _p: "http://10.0.0.7/mcp")},
    )
    for url, _port in _serve(handler):
        with pytest.raises(OutboundBlocked) as caught:
            async with McpSession("", [], transport="http", server_url=f"{url}/mcp", timeout_s=5):
                pass
        assert str(caught.value) == "This address is not allowed."
        assert len(handler.seen) == 1


async def test_a_remote_mcp_server_is_reached_at_the_checked_address(
    allow_loopback_outbound: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is resolved once, by the guard, and the connection goes to
    that address: httpx's own resolver never sees `mcp.pinned.test` (it
    could not resolve it at all), and the server still gets the real Host."""
    monkeypatch.setattr(
        outbound, "resolve_ips", lambda host: ["127.0.0.1"] if host == "mcp.pinned.test" else []
    )
    handler = type("H", (_Handler,), {"seen": []})
    for _url, port in _serve(handler):
        with pytest.raises(BaseException):  # noqa: B017 -- the server is no MCP server
            async with McpSession(
                "",
                [],
                transport="http",
                server_url=f"http://mcp.pinned.test:{port}/mcp",
                timeout_s=5,
            ):
                pass
        assert handler.seen, "nothing reached the checked address"
        assert handler.seen[0]["host"] == f"mcp.pinned.test:{port}"


async def test_a_remote_mcp_redirect_to_another_host_drops_the_custom_auth_header(
    allow_loopback_outbound: None,
) -> None:
    target = type("T", (_Handler,), {"seen": []})
    for target_url, _port in _serve(target):
        source = type(
            "S",
            (_Handler,),
            {"seen": [], "redirect_to": staticmethod(lambda _p, u=target_url: f"{u}/mcp")},
        )
        for source_url, _sport in _serve(source):
            with pytest.raises(BaseException):  # noqa: B017 -- the target is no MCP server
                async with McpSession(
                    "",
                    [],
                    transport="http",
                    server_url=f"{source_url}/mcp",
                    headers={"X-Api-Key": _KEY},
                    timeout_s=5,
                ):
                    pass
            assert source.seen[0].get("x-api-key") == _KEY
            assert target.seen, "the redirect was not followed"
            assert "x-api-key" not in target.seen[0]


# --- logs -------------------------------------------------------------------


async def test_httpx_does_not_log_a_tool_call_url(
    server: str, caplog: pytest.LogCaptureFixture
) -> None:
    """httpx logs each request URL at INFO; a tool call's query string can
    carry a login, so those lines are dropped for tool calls."""
    caplog.set_level("INFO", logger="httpx")
    async with HttpToolSession(server, [_tool("/whoami")]) as session:
        await session.call("t", {"api_key": _KEY})
    assert not [r for r in caplog.records if r.name == "httpx"]
    assert _KEY not in caplog.text
