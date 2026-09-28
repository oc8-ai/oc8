"""Batch POST /tools: sequential dispatch, early stop on suspend, /tool compat."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest


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
    parallel_tool_calls: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Agent + run bound to an odoo MCP connection. Returns (agent_id, run_id, conn_id)."""
    from oc8 import models as m

    dept = m.Department(
        tenant_id=tenant,
        name="Sales",
        frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
    )
    db.add(dept)
    await db.flush()
    model_config_id = None
    if parallel_tool_calls:
        model_config = m.ModelConfig(
            tenant_id=tenant,
            provider="fake",
            model="fake",
            params={"parallel_tool_calls": True},
        )
        db.add(model_config)
        await db.flush()
        model_config_id = model_config.id
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
        model_config_id=model_config_id,
    )
    db.add(agent)
    await db.flush()
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="odoo",
        transport="stdio",
        server_url="stdio://odoo",
        connected=True,
        config={"command": "x", "args": []},
        scopes={"read": ["search_records"], "write": ["create_record"]},
    )
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Quote",
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


async def _post_tools(
    tenant: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    calls: list[dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    token = _agent_token(tenant, agent_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/tools",
                json={"calls": calls},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code, (r.json() if r.content else {})


async def _post_tool(
    tenant: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "c1",
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
                json={"id": call_id, "name": name, "arguments": arguments},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code, (r.json() if r.content else {})


@pytest.mark.asyncio
async def test_tools_batch_returns_results_in_call_order(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs: list[str] = []

    async def fake_call(connection_id: uuid.UUID, *, tool: str = "", **kwargs: Any) -> str:
        label = f"ok:{tool}:{kwargs.get('arguments', {}).get('model', '')}"
        outputs.append(label)
        return label

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(db, tenant)

    code, body = await _post_tools(
        tenant,
        agent_id,
        run_id,
        [
            {
                "id": "c1",
                "name": "search_records",
                "arguments": {"model": "crm.lead"},
            },
            {
                "id": "c2",
                "name": "search_records",
                "arguments": {"model": "res.partner"},
            },
        ],
    )

    assert code == 200, body
    results = body["results"]
    assert len(results) == 2
    assert all(r["status"] == "ok" for r in results)
    assert "ok:search_records:crm.lead" in results[0]["output"]
    assert "ok:search_records:res.partner" in results[1]["output"]
    assert outputs == [
        "ok:search_records:crm.lead",
        "ok:search_records:res.partner",
    ]


@pytest.mark.asyncio
async def test_tools_batch_stops_when_first_waits_for_input(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_calls = 0

    async def fake_call(connection_id: uuid.UUID, **kwargs: Any) -> str:
        nonlocal mcp_calls
        mcp_calls += 1
        return "ok"

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(db, tenant)

    code, body = await _post_tools(
        tenant,
        agent_id,
        run_id,
        [
            {
                "id": "c1",
                "name": "ask_user",
                "arguments": {"question": "Which account should I use?"},
            },
            {
                "id": "c2",
                "name": "search_records",
                "arguments": {"model": "crm.lead"},
            },
        ],
    )

    assert code == 200, body
    results = body["results"]
    assert len(results) == 1
    assert results[0]["status"] == "waiting_for_input"
    assert mcp_calls == 0


@pytest.mark.asyncio
async def test_single_tool_route_still_works(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_call(connection_id: uuid.UUID, **kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(db, tenant)

    code, body = await _post_tool(
        tenant, agent_id, run_id, "search_records", {"model": "crm.lead"}
    )
    assert code == 200, body
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_parallel_reads_overlap_and_keep_call_order(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    seen = 0

    async def fake_call(connection_id: uuid.UUID, *, tool: str = "", **kwargs: Any) -> str:
        nonlocal seen
        seen += 1
        if seen >= 2:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1.0)
        model = kwargs.get("arguments", {}).get("model", "")
        return f"ok:{tool}:{model}"

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(
            db, tenant, parallel_tool_calls=True
        )

    code, body = await _post_tools(
        tenant,
        agent_id,
        run_id,
        [
            {"id": "c1", "name": "search_records", "arguments": {"model": "crm.lead"}},
            {"id": "c2", "name": "search_records", "arguments": {"model": "res.partner"}},
        ],
    )

    assert code == 200, body
    results = body["results"]
    assert [r["status"] for r in results] == ["ok", "ok"]
    assert "crm.lead" in results[0]["output"]
    assert "res.partner" in results[1]["output"]
    assert seen == 2


@pytest.mark.asyncio
async def test_parallel_reads_do_not_start_a_later_write_or_a_call_after_suspend(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    async def fake_call(connection_id: uuid.UUID, *, tool: str = "", **kwargs: Any) -> str:
        order.append(tool)
        if tool == "search_records":
            await asyncio.sleep(0.05)
        return "ok"

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(
            db, tenant, parallel_tool_calls=True
        )

    code, body = await _post_tools(
        tenant,
        agent_id,
        run_id,
        [
            {"id": "c1", "name": "search_records", "arguments": {"model": "crm.lead"}},
            {"id": "c2", "name": "search_records", "arguments": {"model": "res.partner"}},
            {"id": "c3", "name": "create_record", "arguments": {"model": "crm.lead"}},
        ],
    )

    assert code == 200, body
    assert len(body["results"]) == 3
    assert order == ["search_records", "search_records", "create_record"]

    order.clear()
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id, _conn_id = await _seed_mcp_run(
            db, tenant, parallel_tool_calls=True
        )

    code, body = await _post_tools(
        tenant,
        agent_id,
        run_id,
        [
            {"id": "c1", "name": "search_records", "arguments": {"model": "crm.lead"}},
            {"id": "c2", "name": "ask_user", "arguments": {"question": "Which one?"}},
            {"id": "c3", "name": "search_records", "arguments": {"model": "res.partner"}},
        ],
    )

    assert code == 200, body
    assert [r["status"] for r in body["results"]] == ["ok", "waiting_for_input"]
    assert order == ["search_records"]
