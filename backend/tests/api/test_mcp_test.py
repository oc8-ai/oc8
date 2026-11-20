from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_DEMO_FS = str(
    Path(__file__).resolve().parents[2] / "mcp_servers" / "demo_fs.py"
)  # backend/mcp_servers/demo_fs.py — adjust parents[N] if the path differs; assert it exists


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _make_conn(
    db: AsyncSession, tenant: uuid.UUID, command: str, args: list[str]
) -> uuid.UUID:
    conn = m.McpConnection(
        tenant_id=tenant,
        name="c",
        server_url="",
        transport="stdio",
        scopes=[],
        config={"command": command, "args": args},
        connected=False,
    )
    db.add(conn)
    await db.flush()
    return conn.id


async def test_demo_fs_test_succeeds_and_discovers_tools(app_session: AppSessionFactory) -> None:
    assert Path(_DEMO_FS).exists(), _DEMO_FS  # noqa: ASYNC240 - a one-off existence check, not I/O on the hot path
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _make_conn(db, tenant, sys.executable, [_DEMO_FS])
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["connected"] is True
            assert body["health"]["status"] == "ok"
            assert set(body["health"]["tools"]) == {"list_files", "read_file", "write_file"}
            assert body["health"]["toolCount"] == 3

            # persisted
            g = await c.get("/api/v1/mcp/connections", headers=_h(tenant))
            row = next(x for x in g.json() if x["id"] == str(conn_id))
            assert row["connected"] is True


async def test_bad_command_records_error_health(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _make_conn(db, tenant, "/nonexistent/binary/xyz", [])
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant))
            assert r.status_code == 200, r.text  # the test ran; the server failed
            body = r.json()
            assert body["connected"] is False
            assert body["health"]["status"] == "error"
            assert body["health"]["error"]


async def test_test_requires_admin(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _make_conn(db, tenant, "x", [])
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant, "member")
            )
            assert r.status_code == 403


async def test_test_missing_connection_404() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{uuid.uuid4()}/test", headers=_h(tenant))
            assert r.status_code == 404


async def test_timeout_records_error(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A command that sleeps well past a shrunk timeout must fail fast, not hang.
    import oc8.api.v1.mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_TEST_TIMEOUT_S", 0.5, raising=False)
    tenant = uuid.uuid4()
    sleeper_args = ["-c", "import time; time.sleep(30)"]
    async with app_session(tenant) as db:
        conn_id = await _make_conn(db, tenant, sys.executable, sleeper_args)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant))
            assert r.status_code == 200
            assert r.json()["connected"] is False
            assert r.json()["health"]["status"] == "error"


# A minimal real stdio MCP server for the log-event tests below -- spawned
# fresh per test (test_mcp_client_timeout.py's own pattern), not demo_fs.py,
# so these tests don't depend on that fixture's presence.
_MINI_SERVER = """
try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mini")


@mcp.tool()
def ping() -> str:
    return "pong"


mcp.run()
"""


def _mini_server(tmp_path: Path) -> tuple[str, list[str]]:
    script = tmp_path / "mini_server.py"
    script.write_text(_MINI_SERVER)
    return sys.executable, [str(script)]


class _SpyBus:
    """Same shape as test_internal_agent.py's own spy -- collects every
    published (type, data) pair without touching Redis."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish_event(self, tenant_id: object, type_: str, data: dict, **kw: object) -> None:
        self.published.append((type_, data))


def _patch_bus(monkeypatch: pytest.MonkeyPatch) -> _SpyBus:
    # emit.py resolves get_event_bus via its own module-level import, which
    # patching only oc8.realtime.bus leaves unpatched -- see
    # test_internal_agent.py's identical note.
    spy = _SpyBus()
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: spy)
    monkeypatch.setattr("oc8.realtime.emit.get_event_bus", lambda: spy)
    return spy


async def test_a_successful_test_streams_spawn_handshake_list_tools_and_result(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The log drawer needs these four steps, in order, for the connection
    under test -- not just the final `health` this endpoint already wrote."""
    spy = _patch_bus(monkeypatch)
    tenant = uuid.uuid4()
    command, args = _mini_server(tmp_path)
    async with app_session(tenant) as db:
        conn_id = await _make_conn(db, tenant, command, args)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant))
            assert r.status_code == 200, r.text

    log_events = [data for type_, data in spy.published if type_ == "mcp.test.log"]
    steps = [data["step"] for data in log_events]
    assert steps == ["spawn", "handshake", "list_tools", "result"]
    assert all(data["connection_id"] == str(conn_id) for data in log_events)
    assert "1 tool" in log_events[-1]["message"]


async def test_a_failed_test_publishes_the_same_sanitized_error_as_health(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _patch_bus(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _make_conn(db, tenant, "/nonexistent/binary/xyz", [])
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant))
            assert r.status_code == 200, r.text
            health_error = r.json()["health"]["error"]

    result_events = [data for _, data in spy.published if data.get("step") == "result"]
    assert len(result_events) == 1
    # Same text the operator already sees in `health.error` -- no separate,
    # unaudited error-formatting path for the log line.
    assert health_error in result_events[0]["message"]


async def test_patching_a_connection_answers_200_and_persists(
    app_session: AppSessionFactory,
) -> None:
    """PATCH wrote correctly and then answered 500.

    The handler committed mid-request and refreshed afterwards, but the RLS GUC
    is TRANSACTION-local: after the commit the session is unbound, so the reload
    ran with app.tenant_id = '' and blew up on the uuid cast. The row was already
    saved, so an operator saw a server error for a change that had in fact
    landed -- and the natural response, doing it again, hid the truth further.
    Committing is the session's job here, not the handler's.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _make_conn(db, tenant, "bridge", ["serve"])
        await db.commit()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.patch(
                f"/api/v1/mcp/connections/{conn_id}",
                json={"secretEnv": {"SERVICE_TOKEN": "vault/token"}, "name": "renamed"},
                headers=_h(tenant),
            )
            assert r.status_code == 200, r.text
            assert r.json()["name"] == "renamed"

    async with app_session(tenant) as db:
        stored = await db.get(m.McpConnection, conn_id)
        assert stored is not None
        assert stored.name == "renamed"
        assert stored.config["secret_env"] == {"SERVICE_TOKEN": "vault/token"}


async def _make_manual_http_conn(
    db: AsyncSession, tenant: uuid.UUID, base_url: str, http_tools: list[dict]
) -> uuid.UUID:
    conn = m.McpConnection(
        tenant_id=tenant,
        name="c-http",
        server_url=base_url,
        transport="manual_http",
        scopes=[],
        config={"http_tools": http_tools},
        connected=False,
    )
    db.add(conn)
    await db.flush()
    return conn.id


async def test_manual_http_connection_test_validates_tools_without_network(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    tools = [
        {
            "name": "ping",
            "description": "d",
            "method": "GET",
            "url_template": "/ping",
            "param_schema": {"type": "object", "properties": {}},
        }
    ]
    async with app_session(tenant) as db:
        conn_id = await _make_manual_http_conn(db, tenant, "http://example.invalid", tools)
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["connected"] is True
            assert body["health"]["tools"] == ["ping"]


async def test_manual_http_connection_with_no_tools_fails_the_test(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn_id = await _make_manual_http_conn(db, tenant, "http://example.invalid", [])
        await db.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/mcp/connections/{conn_id}/test", headers=_h(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["connected"] is False
            assert body["health"]["status"] == "error"
