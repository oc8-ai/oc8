"""The three logins a plain HTTP API can ask for, against a real loopback
server: an API key in a header or the query string, Basic, and OAuth2 client
credentials with a cached token that is refetched once on a 401.

The server records what it received: what it answers is redacted before the
agent sees it, so an echoed credential cannot be read back from the body."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest

import oc8.agent.http_tools as ht
from oc8.agent.http_tools import (
    HttpLogin,
    HttpToolError,
    HttpToolSession,
    forget_token,
    oauth2_token,
    resolve_http_login,
)

_STATE: dict[str, object] = {}
_LOCK = threading.Lock()


def _count(key: str) -> int:
    with _LOCK:
        _STATE[key] = int(_STATE.get(key, 0)) + 1  # type: ignore[call-overload]
        return int(_STATE[key])  # type: ignore[call-overload]


class _Handler(BaseHTTPRequestHandler):
    redirect_target = ""

    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, body: dict[str, object] | str, location: str = "") -> None:
        raw = (json.dumps(body) if isinstance(body, dict) else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        with _LOCK:
            _STATE.setdefault("posts", []).append(  # type: ignore[union-attr]
                {"path": self.path, "auth": self.headers.get("Authorization"), "body": raw}
            )
        if self.path == "/token":
            form = urllib.parse.parse_qs(raw)
            # Slow enough that simultaneous calls really overlap.
            time.sleep(0.05)
            calls = _count("token_calls")
            auth = self.headers.get("Authorization", "")
            expected = "Basic " + base64.b64encode(b"cid:csecret").decode()
            if auth != expected or form.get("grant_type") != ["client_credentials"]:
                self._send(401, {"error": "invalid_client"})
                return
            self._send(200, {"access_token": f"tok-{calls}", "expires_in": 3600})
        elif self.path == "/token-elsewhere":
            self._send(307, "", location=f"{type(self).redirect_target}/token")
        elif self.path == "/token-to-metadata":
            self._send(307, "", location="http://169.254.169.254/token")
        else:
            self._send(404, {})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        with _LOCK:
            _STATE["auth"] = self.headers.get("Authorization")
            _STATE["key"] = self.headers.get("X-Api-Key")
            _STATE["query"] = urllib.parse.parse_qs(parsed.query)
            _STATE.setdefault("gets", []).append(self.path)  # type: ignore[union-attr]
        if parsed.path == "/whoami":
            self._send(
                200,
                {
                    "auth": self.headers.get("Authorization"),
                    "key": self.headers.get("X-Api-Key"),
                    "query": urllib.parse.parse_qs(parsed.query),
                },
            )
        elif parsed.path == "/stale-once":
            # Rejects the first token it ever sees, accepts the next one.
            with _LOCK:
                seen = _STATE.setdefault("seen", [])
                assert isinstance(seen, list)
                seen.append(self.headers.get("Authorization"))
                first = len(seen) == 1
            if first:
                self._send(401, {"error": "expired"})
            else:
                self._send(200, {"ok": True})
        elif parsed.path == "/echo-url":
            self._send(500, f"failed for {self.path}")
        elif parsed.path == "/to-same":
            self._send(302, "", location="/whoami")
        elif parsed.path == "/to-elsewhere":
            self._send(302, "", location=f"{type(self).redirect_target}/whoami")
        else:
            self._send(404, {})


def _serve(handler: type[_Handler]) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def other(allow_loopback_outbound: None) -> Iterator[tuple[str, dict[str, list[str]]]]:
    """A second server on another port: another origin. Records every path
    and every credential it was sent."""
    seen: dict[str, list[str]] = {"paths": [], "auth": []}

    class _Other(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _any(self) -> None:
            seen["paths"].append(self.path)
            seen["auth"].append(str(self.headers.get("Authorization")))
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                seen["auth"].append(self.rfile.read(length).decode())
            body = b'{"access_token": "stolen", "expires_in": 3600}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _any
        do_POST = _any

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Other)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def server(allow_loopback_outbound: None) -> Iterator[str]:
    _STATE.clear()
    handler = type("H", (_Handler,), {})
    try:
        yield from _serve(handler)
    finally:
        ht._TOKENS.clear()


@pytest.fixture
def servers(
    allow_loopback_outbound: None, other: tuple[str, dict[str, list[str]]]
) -> Iterator[tuple[str, dict[str, list[str]]]]:
    _STATE.clear()
    handler = type("H", (_Handler,), {"redirect_target": other[0]})
    try:
        for url in _serve(handler):
            yield url, other[1]
    finally:
        ht._TOKENS.clear()


def _tools(*paths: str) -> list[dict[str, object]]:
    return [
        {"name": p.strip("/").replace("-", "_"), "method": "GET", "url_template": p} for p in paths
    ]


def _oauth(server: str, *, secret: str = "csecret", path: str = "/token") -> HttpLogin:
    return HttpLogin(
        kind="oauth2_client_credentials",
        secret=secret,
        token_url=f"{server}{path}",
        client_id="cid",
    )


# --- resolving ---------------------------------------------------------------


def test_resolve_reads_config_and_secret_env() -> None:
    cfg = {"auth": {"kind": "api_key", "in": "query", "name": "api_key"}}
    login = resolve_http_login(cfg, {"HTTP_AUTH_SECRET": "k1"})
    assert login == HttpLogin(kind="api_key", secret="k1", location="query", name="api_key")
    assert resolve_http_login({}, {"HTTP_AUTH_SECRET": "k1"}) is None
    assert resolve_http_login({"auth": {"kind": "cookie"}}, {"HTTP_AUTH_SECRET": "x"}) is None
    assert "k1" not in repr(login)


def test_resolve_basic_and_oauth2() -> None:
    basic = resolve_http_login(
        {"auth": {"kind": "basic", "username": "ada"}}, {"HTTP_AUTH_SECRET": "pw"}
    )
    assert basic == HttpLogin(kind="basic", secret="pw", username="ada")
    oauth = resolve_http_login(
        {
            "auth": {
                "kind": "oauth2_client_credentials",
                "token_url": "https://auth.example.com/t",
                "client_id": "cid",
                "scope": "read",
            }
        },
        {"HTTP_AUTH_SECRET": "cs"},
    )
    assert oauth == HttpLogin(
        kind="oauth2_client_credentials",
        secret="cs",
        token_url="https://auth.example.com/t",
        client_id="cid",
        scope="read",
    )


# --- sending ------------------------------------------------------------------


async def test_api_key_in_a_header(server: str) -> None:
    login = HttpLogin(kind="api_key", secret="k-123", name="X-Api-Key")
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        body = json.loads(await s.call("whoami", {}))
    assert _STATE["key"] == "k-123"
    # Echoed back, it is redacted before the agent sees it.
    assert body["key"] == "[redacted]"


async def test_api_key_in_the_query(server: str) -> None:
    login = HttpLogin(kind="api_key", secret="k-123", location="query", name="api_key")
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        body = json.loads(await s.call("whoami", {}))
    assert _STATE["query"] == {"api_key": ["k-123"]}
    assert "k-123" not in json.dumps(body)


async def test_a_tool_argument_cannot_replace_the_query_key(server: str) -> None:
    login = HttpLogin(kind="api_key", secret="k-123", location="query", name="api_key")
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        await s.call("whoami", {"api_key": "from-the-model"})
    assert _STATE["query"] == {"api_key": ["k-123"]}


async def test_the_query_key_survives_a_same_origin_redirect(server: str) -> None:
    login = HttpLogin(kind="api_key", secret="k-123", location="query", name="api_key")
    async with HttpToolSession(server, _tools("/to-same"), login=login) as s:
        await s.call("to_same", {})
    assert _STATE["query"] == {"api_key": ["k-123"]}


async def test_the_query_key_is_not_sent_to_another_origin(
    servers: tuple[str, dict[str, list[str]]],
) -> None:
    server, seen = servers
    login = HttpLogin(kind="api_key", secret="k-123", location="query", name="api_key")
    async with HttpToolSession(server, _tools("/to-elsewhere"), login=login) as s:
        await s.call("to_elsewhere", {})
    assert seen["paths"] == ["/whoami"]


async def test_basic(server: str) -> None:
    login = HttpLogin(kind="basic", secret="pw-secret", username="ada")
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        body = json.loads(await s.call("whoami", {}))
    credentials = base64.b64encode(b"ada:pw-secret").decode()
    assert _STATE["auth"] == f"Basic {credentials}"
    assert credentials not in json.dumps(body)


async def test_basic_is_not_sent_to_another_origin(
    servers: tuple[str, dict[str, list[str]]],
) -> None:
    server, seen = servers
    login = HttpLogin(kind="basic", secret="pw-secret", username="ada")
    async with HttpToolSession(server, _tools("/to-elsewhere"), login=login) as s:
        await s.call("to_elsewhere", {})
    assert seen["paths"] == ["/whoami"]
    assert seen["auth"] == ["None"]


# --- OAuth2 client credentials ------------------------------------------------


async def test_oauth2_token_is_fetched_once_and_cached(server: str) -> None:
    login = _oauth(server)
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        first = await s.call("whoami", {})
        assert _STATE["auth"] == "Bearer tok-1"
        second = await s.call("whoami", {})
        assert _STATE["auth"] == "Bearer tok-1"
    assert _STATE["token_calls"] == 1
    # The access token an API echoes is redacted too.
    assert "tok-1" not in first + second


async def test_simultaneous_calls_fetch_one_token(server: str) -> None:
    login = _oauth(server)
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        await asyncio.gather(*(s.call("whoami", {}) for _ in range(6)))
    assert _STATE["token_calls"] == 1


async def test_a_changed_secret_or_client_or_token_url_gets_a_new_token(server: str) -> None:
    first = await oauth2_token(_oauth(server), timeout_s=5)
    assert first == "tok-1"
    assert await oauth2_token(_oauth(server), timeout_s=5) == "tok-1"
    assert ht._token_key(_oauth(server)) != ht._token_key(_oauth(server, secret="rotated"))
    assert ht._token_key(_oauth(server)) != ht._token_key(
        HttpLogin(
            kind="oauth2_client_credentials",
            secret="csecret",
            token_url=f"{server}/token",
            client_id="other",
        )
    )
    assert ht._token_key(_oauth(server)) != ht._token_key(_oauth(server, path="/token2"))
    with pytest.raises(HttpToolError):
        # A rotated secret does not reuse the token cached for the old one.
        await oauth2_token(_oauth(server, secret="rotated"), timeout_s=5)


async def test_oauth2_token_is_refetched_after_expiry(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ht, "_now", lambda: clock[0])
    login = _oauth(server)
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        await s.call("whoami", {})
        clock[0] += 3600 - 60 - 1  # still inside expires_in minus the margin
        await s.call("whoami", {})
        assert _STATE["auth"] == "Bearer tok-1"
        clock[0] += 2  # past it
        await s.call("whoami", {})
        assert _STATE["auth"] == "Bearer tok-2"


async def test_a_401_refetches_the_token_once_and_retries(server: str) -> None:
    login = _oauth(server)
    async with HttpToolSession(server, _tools("/stale-once"), login=login) as s:
        body = json.loads(await s.call("stale_once", {}))
    assert body == {"ok": True}
    assert _STATE["seen"] == ["Bearer tok-1", "Bearer tok-2"]


async def test_a_token_failure_is_its_own_error_class(server: str) -> None:
    login = _oauth(server, secret="wrong-secret")
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        with pytest.raises(HttpToolError) as caught:
            await s.call("whoami", {})
    assert caught.value.kind == "token"
    assert "wrong-secret" not in str(caught.value)
    # client_secret_basic first, then client_secret_post once.
    assert _STATE["token_calls"] == 2


async def test_token_url_is_guarded(server: str) -> None:
    login = HttpLogin(
        kind="oauth2_client_credentials",
        secret="csecret",
        token_url="http://169.254.169.254/token",
        client_id="cid",
    )
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        with pytest.raises(HttpToolError) as caught:
            await s.call("whoami", {})
    assert caught.value.kind == "blocked"


async def test_token_url_redirect_to_metadata_is_blocked(server: str) -> None:
    login = _oauth(server, path="/token-to-metadata")
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        with pytest.raises(HttpToolError) as caught:
            await s.call("whoami", {})
    assert caught.value.kind == "blocked"


async def test_token_credentials_never_follow_a_redirect_to_another_origin(
    servers: tuple[str, dict[str, list[str]]],
) -> None:
    server, seen = servers
    login = _oauth(server, path="/token-elsewhere")
    async with HttpToolSession(server, _tools("/whoami"), login=login) as s:
        with pytest.raises(HttpToolError) as caught:
            await s.call("whoami", {})
    assert caught.value.kind == "token"
    assert seen["paths"] == []
    assert "stolen" not in str(_STATE.get("auth"))


# --- secrets never in errors or logs -------------------------------------------


async def test_query_key_is_never_in_error_text_or_logs(
    server: str, caplog: pytest.LogCaptureFixture
) -> None:
    login = HttpLogin(kind="api_key", secret="sk-QUERY-SECRET", location="query", name="api_key")
    caplog.set_level(logging.DEBUG)
    async with HttpToolSession(server, _tools("/echo-url"), login=login) as s:
        with pytest.raises(HttpToolError) as caught:
            await s.call("echo_url", {})
    assert _STATE["query"] == {"api_key": ["sk-QUERY-SECRET"]}
    assert "sk-QUERY-SECRET" not in str(caught.value)
    assert "sk-QUERY-SECRET" not in caplog.text


async def test_token_request_secrets_never_reach_the_logs(
    server: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with HttpToolSession(server, _tools("/whoami"), login=_oauth(server)) as s:
        await s.call("whoami", {})
    assert "csecret" not in caplog.text
    assert base64.b64encode(b"cid:csecret").decode() not in caplog.text
    assert "tok-1" not in caplog.text


def test_httpx2_request_lines_never_show_a_query_string(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx2 (under the MCP SDK) logs each request URL at INFO; a server
    URL may carry a key in its query string, so its value is hidden."""
    caplog.set_level(logging.INFO, logger="httpx2")
    logging.getLogger("httpx2").info(
        'HTTP Request: %s %s "%s %d %s"',
        "POST",
        httpx2.URL("https://mcp.example.com/mcp?token=sk-MCP-SECRET"),
        "HTTP/1.1",
        200,
        "OK",
    )
    assert "sk-MCP-SECRET" not in caplog.text
    assert "https://mcp.example.com/mcp" in caplog.text


def test_forget_token_drops_the_cache_entry() -> None:
    login = HttpLogin(
        kind="oauth2_client_credentials", secret="s", token_url="https://a/t", client_id="c"
    )
    ht._TOKENS[ht._token_key(login)] = ("t", 1e12)
    forget_token(login)
    assert ht._token_key(login) not in ht._TOKENS
