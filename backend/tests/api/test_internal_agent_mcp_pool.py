"""Isolated /tool and /step must reuse mcp_pool instead of a fresh McpSession."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8.modelrouter.types import NeutralTool


def _agent_token(tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from oc8.auth import get_identity_provider

    return get_identity_provider().mint(
        tenant_id=tenant,
        subject=f"agent:{agent_id}",
        role="agent_default",
        kind="agent",
        scopes=[f"run:{run_id}"],
    )


async def _seed_mcp_run(
    db: Any,
    tenant: uuid.UUID,
    *,
    oauth: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Agent + run bound to an odoo MCP connection. Returns (agent_id, run_id, conn_id)."""
    from oc8 import models as m

    dept = m.Department(
        tenant_id=tenant,
        name="Vertrieb",
        frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
    )
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    cfg: dict[str, Any] = {"command": "x", "args": []}
    if oauth:
        cfg["secret_env"] = {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"}
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="odoo",
        transport="stdio",
        server_url="stdio://odoo",
        connected=True,
        config=cfg,
        scopes={"read": ["search_records"], "write": ["create_record"]},
    )
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Angebot",
        state="in_progress",
    )
    db.add_all([conn, task])
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        state="running",
        context={"task": "x", "mcp_connection_id": str(conn.id)},
    )
    db.add(run)
    await db.flush()
    return agent.id, run.id, conn.id


async def _post_tool(
    tenant: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str,
    arguments: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    token = _agent_token(tenant, agent_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/tool",
                json={"id": "c1", "name": name, "arguments": arguments},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code, (r.json() if r.content else {})


async def _post_finish(
    tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[int, dict[str, Any]]:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    token = _agent_token(tenant, agent_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/finish",
                json={"status": "done", "output": "fertig"},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code, (r.json() if r.content else {})


async def _post_step(
    tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[int, dict[str, Any]]:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    token = _agent_token(tenant, agent_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code, (r.json() if r.content else {})


class _ForbiddenSession:
    """Raised if /tool or /step still builds a direct McpSession."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        raise AssertionError("McpSession must not be constructed; use mcp_pool")


@pytest.mark.asyncio
async def test_two_tool_calls_share_one_pool_call_setup(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second /tool must pass reusable=True for a non-oauth cfg."""
    seen: list[bool] = []

    async def fake_call(connection_id: uuid.UUID, *, reusable: bool = True, **kwargs: Any) -> str:
        seen.append(reusable)
        return "ok"

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)
    monkeypatch.setattr("oc8.agent.mcp_pool.McpSession", _ForbiddenSession)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(db, tenant)

    code1, body1 = await _post_tool(
        tenant, agent_id, run_id, "search_records", {"model": "crm.lead"}
    )
    code2, body2 = await _post_tool(
        tenant, agent_id, run_id, "search_records", {"model": "crm.lead"}
    )

    assert code1 == 200 and code2 == 200, (body1, body2)
    assert seen == [True, True]


@pytest.mark.asyncio
async def test_oauth_tool_call_passes_reusable_false(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    async def fake_call(connection_id: uuid.UUID, *, reusable: bool = True, **kwargs: Any) -> str:
        seen.append(reusable)
        return "ok"

    async def fake_env(*a: Any, **kw: Any) -> dict[str, str]:
        return {"GRAPH_ACCESS_TOKEN": "minted"}

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)
    monkeypatch.setattr("oc8.api.v1.internal_agent._mcp_env", fake_env)
    monkeypatch.setattr("oc8.agent.mcp_pool.McpSession", _ForbiddenSession)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(db, tenant, oauth=True)

    code, body = await _post_tool(
        tenant, agent_id, run_id, "search_records", {"model": "crm.lead"}
    )
    assert code == 200, body
    assert seen == [False]


@pytest.mark.asyncio
async def test_finish_closes_the_pool(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[uuid.UUID] = []

    async def fake_close(connection_id: uuid.UUID) -> None:
        closed.append(connection_id)

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.close", fake_close)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, conn_id = await _seed_mcp_run(db, tenant)

    code, body = await _post_finish(tenant, agent_id, run_id)
    assert code == 200, body
    assert closed == [conn_id]


@pytest.mark.asyncio
async def test_first_step_lists_tools_through_the_pool(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema discovery on the first /step must go through mcp_pool.tools."""
    from oc8.modelrouter.types import CompletionResult, Usage

    seen: list[bool] = []

    async def fake_tools(
        connection_id: uuid.UUID, *, reusable: bool = True, **kwargs: Any
    ) -> list[NeutralTool]:
        seen.append(reusable)
        return [
            NeutralTool(
                name="search_records",
                description="search",
                parameters={"type": "object", "properties": {}},
            )
        ]

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        return CompletionResult(
            text="fertig",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    def _as_stream(fake: Any) -> Any:
        from oc8.modelrouter import chunk_from_result

        async def _stream(*args: object, **kw: object) -> Any:
            yield chunk_from_result(await fake(*args, **kw))

        return _stream

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.tools", fake_tools)
    monkeypatch.setattr("oc8.agent.mcp_pool.McpSession", _ForbiddenSession)
    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(db, tenant)

    code, body = await _post_step(tenant, agent_id, run_id)
    assert code == 200, body
    assert seen == [True]
