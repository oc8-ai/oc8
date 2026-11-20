"""The isolated runtime offers only what the frame lets through -- with ONE
connection too.

Live 2026-11-13 (runs 01a1c103, 01a1d471): a department with a single
microsoft365 connection whose `only` names files/word/excel tools; the first
/step listed all 35 of the server's tools, mail_* included, because the frame
filter ran only when the department had several systems. The model called
mail_search and the gate denied it -- every time.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.modelrouter.types import CompletionResult, NeutralTool, Usage
from tests.api.test_internal_agent_mcp_pool import _agent_token, _ForbiddenSession, _post_step

pytestmark = pytest.mark.asyncio

SCOPES = {
    "read": ["files_list", "files_search", "mail_search", "mail_get"],
    "modify": ["files_upload", "mail_send"],
}
SERVER_TOOLS = [
    "files_list",
    "files_search",
    "files_upload",
    "mail_search",
    "mail_get",
    "mail_send",
]


async def _seed(
    db: Any,
    tenant: uuid.UUID,
    *,
    frame_policy: dict[str, Any],
    narrowing: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    dept = m.Department(
        tenant_id=tenant, name="General", frame={"tools": {"microsoft365": frame_policy}}
    )
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Umsatz-Reporter",
        status="running",
        narrowing=narrowing or {},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="microsoft365",
        transport="stdio",
        server_url="stdio://m365",
        connected=True,
        config={"command": "x", "args": []},
        scopes=SCOPES,
    )
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Bericht",
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
    return agent.id, run.id


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tools(connection_id: uuid.UUID, **kwargs: Any) -> list[NeutralTool]:
        return [
            NeutralTool(name=n, description=n, parameters={"type": "object", "properties": {}})
            for n in SERVER_TOOLS
        ]

    async def _stream(*args: object, **kw: object) -> Any:
        from oc8.modelrouter import chunk_from_result

        yield chunk_from_result(
            CompletionResult(
                text="fertig",
                tool_calls=[],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="stop",
                provider="fake",
                model="fake",
            )
        )

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.tools", fake_tools)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _ForbiddenSession)
    monkeypatch.setattr("oc8.api.v1.internal_agent.stream_completion_with_fallback", _stream)


async def _offered_names(
    app_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    frame_policy: dict[str, Any],
    narrowing: dict[str, Any] | None = None,
) -> set[str]:
    _patch(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _seed(db, tenant, frame_policy=frame_policy, narrowing=narrowing)
    code, body = await _post_step(tenant, agent_id, run_id)
    assert code == 200, body
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        return {t["name"] for t in run.context["tool_schemas"]}


async def test_a_single_connection_is_filtered_by_the_frame_only(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = await _offered_names(
        app_session,
        monkeypatch,
        frame_policy={
            "enabled": True,
            "read": True,
            "modify": True,
            "only": ["files_list", "files_search", "files_upload"],
        },
    )
    assert names == {"files_list", "files_search", "files_upload"}


async def test_a_single_connection_without_modify_offers_no_writing_tool(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = await _offered_names(
        app_session, monkeypatch, frame_policy={"enabled": True, "read": True, "modify": False}
    )
    assert names == {"files_list", "files_search", "mail_search", "mail_get"}


async def test_the_agents_narrowing_only_narrows_a_single_connection(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = await _offered_names(
        app_session,
        monkeypatch,
        frame_policy={"enabled": True, "read": True, "modify": True},
        narrowing={
            "tools": {
                "microsoft365": {
                    "enabled": True,
                    "read": True,
                    "modify": True,
                    "only": ["files_list", "mail_search"],
                }
            }
        },
    )
    assert names == {"files_list", "mail_search"}


async def test_an_approval_gated_tool_is_still_offered(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = await _offered_names(
        app_session,
        monkeypatch,
        frame_policy={
            "enabled": True,
            "read": True,
            "modify": True,
            "only": ["files_list", "files_upload"],
            "approval_actions": ["files_upload"],
        },
    )
    assert names == {"files_list", "files_upload"}


async def test_a_hidden_tool_named_anyway_is_still_denied(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    called: list[str] = []

    async def fake_call(connection_id: uuid.UUID, **kwargs: Any) -> str:
        called.append(str(kwargs.get("name")))
        return "ok"

    monkeypatch.setattr("oc8.api.v1.internal_agent.mcp_pool.call", fake_call)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _seed(
            db,
            tenant,
            frame_policy={"enabled": True, "read": True, "modify": True, "only": ["files_list"]},
        )
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/tool",
                json={"id": "c1", "name": "mail_search", "arguments": {}},
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            )
    assert r.status_code == 200, r.text
    assert called == []
    assert r.json()["status"] == "denied"
