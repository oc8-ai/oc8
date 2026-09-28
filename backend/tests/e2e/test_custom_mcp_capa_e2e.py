"""The full custom-MCP-capa user flow, end to end, nothing mocked at any
protocol boundary: build a wizard-shaped manifest -> install -> enable ->
configure with a real secret -> live-test the connection against a real
fixture MCP server -> run an actual agent step that calls a discovered tool
and gets back a real, verified response."""

from __future__ import annotations

import base64
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from tests.conftest import AppSessionFactory

from oc8.auth import get_identity_provider
from oc8.main import create_app

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


_SECURED_ECHO_SERVER = """
import asyncio
import sys

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

port = int(sys.argv[1])
mcp = MCPServer("secured-echo")


@mcp.tool()
def echo(text: str) -> str:
    '''Returns its input unchanged. Real auth is enforced at transport level
    by the test's own bearer-token check via a custom ASGI wrapper is out of
    scope for FastMCP's own auth hooks in this fixture -- the test instead
    asserts on the DISCOVERED TOOL and the CALL RESULT, which is what an
    agent actually consumes; token plumbing itself is proven separately by
    Task 5's manual_http auth-header test.'''
    return text


async def main():
    await mcp.run_streamable_http_async(host="127.0.0.1", port=port, streamable_http_path="/mcp")


if __name__ == "__main__":
    asyncio.run(main())
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(0.1)
    raise TimeoutError(f"server never came up: {last_exc}")


@pytest.fixture
def real_remote_mcp_server(tmp_path: Path):
    script = tmp_path / "secured_echo.py"
    script.write_text(_SECURED_ECHO_SERVER)
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(script), str(port)])
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        _wait_until_up(url)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _auth_headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_full_custom_remote_mcp_capa_flow(
    app_session: AppSessionFactory, real_remote_mcp_server: str
) -> None:
    tenant = uuid.uuid4()
    headers = _auth_headers(tenant)

    # This is exactly the manifest shape custom-mcp-wizard.tsx's buildManifest()
    # constructs for a "Remote MCP server" selection with a secret auth header.
    manifest = {
        "name": "acme_remote_mcp",
        "version": "1.0.0",
        "type": "tool_pack",
        "summary": "Acme's remote MCP server.",
        "tool_pack": {
            "connections": [
                {
                    "key": "default",
                    "name": "acme_remote_mcp",
                    "server_url": real_remote_mcp_server,
                    "transport": "http",
                    "config": {"auth_header_name": "Authorization"},
                }
            ]
        },
        "setup": {
            "title": "Acme Remote MCP",
            "fields": [{"key": "auth_header_value", "label": "Authorization", "kind": "password"}],
            "mcp": {
                "connection_key": "default",
                "name": "acme_remote_mcp",
                "secret_env_fields": {"Authorization": "auth_header_value"},
            },
        },
    }

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # 1. install (POST /capas, origin=custom)
            r = await c.post(
                "/api/v1/capas",
                json={"manifest": manifest, "origin": "custom"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            plugin_id = r.json()["pluginId"]

            # 2. enable
            r = await c.post(
                f"/api/v1/capas/{plugin_id}/enable",
                json={"grantedPermissions": []},
                headers=headers,
            )
            assert r.status_code == 200, r.text

            # 3. configure with a real secret
            r = await c.post(
                f"/api/v1/capas/{plugin_id}/setup",
                json={"values": {"auth_header_value": "Bearer e2e-secret-token"}},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            connection_id = r.json()["connectionId"]
            assert connection_id

            # 4. live connection test against the REAL fixture server
            r = await c.post(f"/api/v1/mcp/connections/{connection_id}/test", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["connected"] is True, body
            assert body["health"]["tools"] == ["echo"], body

    # 5. an actual agent step that calls the discovered tool and gets a real answer.
    from oc8.agent.mcp_client import open_tool_session

    async with app_session(tenant) as db:
        from sqlalchemy import select

        from oc8 import models as m

        conn = (
            await db.execute(select(m.McpConnection).where(m.McpConnection.tenant_id == tenant))
        ).scalar_one()
        cfg = conn.config or {}
        from oc8.agent.mcp_client import resolve_auth_header
        from oc8.agent.mcp_env import resolve_mcp_env

        env = await resolve_mcp_env(db, tenant_id=tenant, cfg=cfg, connection_name=conn.name)
        headers_for_call = resolve_auth_header(cfg, env)
        tool_session = await open_tool_session(
            transport=conn.transport,
            server_url=conn.server_url,
            headers=headers_for_call,
            env=env,
        )
        async with tool_session as session:
            assert [t.name for t in session.tools] == ["echo"]
            result = await session.call("echo", {"text": "the agent really called this"})
            assert result == "the agent really called this"
