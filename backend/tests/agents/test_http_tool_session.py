"""HttpToolSession must be a real drop-in for McpSession over a plain REST
API with no MCP support at all -- proven against a real local HTTP server,
not a mock, so URL templating, query/body placement, and error mapping are
all exercised for real."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from oc8.agent.mcp_client import HttpToolSession

pytestmark = pytest.mark.asyncio


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:  # quiet test output
        return

    def do_GET(self) -> None:
        if self.path.startswith("/users/"):
            user_id = self.path.split("/users/")[1]
            if self.headers.get("Authorization") != "Bearer secret-token":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"unauthorized")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(f'{{"id": "{user_id}", "name": "Ada"}}'.encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/users":
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal error")
            return
        self.send_response(404)
        self.end_headers()


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


_TOOLS = [
    {
        "name": "get_user",
        "description": "Fetch a user by id.",
        "method": "GET",
        "url_template": "/users/{id}",
        "param_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "create_user",
        "description": "Create a user.",
        "method": "POST",
        "url_template": "/users",
        "param_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
    {
        "name": "boom",
        "description": "Always fails.",
        "method": "POST",
        "url_template": "/boom",
        "param_schema": {"type": "object", "properties": {}},
    },
]


async def test_discovers_tools_without_any_handshake(http_server: str) -> None:
    async with HttpToolSession(http_server, _TOOLS) as session:
        assert {t.name for t in session.tools} == {"get_user", "create_user", "boom"}


async def test_url_template_and_auth_header(http_server: str) -> None:
    async with HttpToolSession(
        http_server, _TOOLS, headers={"Authorization": "Bearer secret-token"}
    ) as session:
        result = await session.call("get_user", {"id": "42"})
        assert '"id": "42"' in result


async def test_missing_auth_header_surfaces_as_error(http_server: str) -> None:
    async with HttpToolSession(http_server, _TOOLS) as session:
        with pytest.raises(RuntimeError, match="401"):
            await session.call("get_user", {"id": "42"})


async def test_post_body_and_5xx_mapping(http_server: str) -> None:
    async with HttpToolSession(http_server, _TOOLS) as session:
        created = await session.call("create_user", {"name": "Ada"})
        assert "Ada" in created
        with pytest.raises(RuntimeError, match="500"):
            await session.call("boom", {})
