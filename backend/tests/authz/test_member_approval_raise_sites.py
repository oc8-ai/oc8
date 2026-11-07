"""Every runtime that holds a tool call routes it the same way (D3a, spec §3):
Anna's own "ask me first" makes her the assignee of the raised approval, and
when the department asks too, the row says a department stage follows. One
test per raise site -- the in-process engine, the MCP gateway and the
isolated runtime's internal API."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.modelrouter import ToolCall
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

ANNA = "anna@x.de"
ASK = {"enabled": True, "read": True, "modify": True, "approval_actions": ["modify"]}
FREE = {"enabled": True, "read": True, "modify": True}


def _frame(key: str, *, department_asks: bool) -> dict[str, Any]:
    return {"tools": {key: ASK if department_asks else FREE}}


async def _anna_asks_first(
    db: AsyncSession, tenant: uuid.UUID, agent_id: uuid.UUID, key: str
) -> uuid.UUID:
    anna = m.OrgMember(tenant_id=tenant, subject=ANNA, subject_uuid=uuid.uuid4())
    db.add(anna)
    await db.flush()
    db.add(
        m.MemberGuardrail(
            tenant_id=tenant,
            member_id=anna.id,
            agent_id=agent_id,
            policy={"tools": {key: ASK}},
        )
    )
    await db.flush()
    return anna.id


async def _held(app_session: AppSessionFactory, tenant: uuid.UUID) -> m.ApprovalRequest:
    async with app_session(tenant) as db:
        rows = list((await db.execute(select(m.ApprovalRequest))).scalars().all())
    assert len(rows) == 1, rows
    return rows[0]


def _assert_routed(row: m.ApprovalRequest, anna: uuid.UUID, *, department_asks: bool) -> None:
    assert row.assignee_member_id == anna
    assert row.action_type == "tool_send"
    assert (row.payload or {}).get("department_stage") is (True if department_asks else None)
    assert row.reason_context is not None
    assert row.reason_context["approval_from"] == (
        ["department", "member"] if department_asks else ["member"]
    )


@pytest.fixture(autouse=True)
async def _no_pooled_sessions() -> Any:
    from oc8.agent import mcp_pool

    await mcp_pool.close_all()
    yield
    await mcp_pool.close_all()


# ------------------------------------------------------------ in-process engine


@pytest.mark.parametrize("department_asks", [True, False])
async def test_the_engine_routes_annas_held_call_to_her(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, department_asks: bool
) -> None:
    from oc8.agent.engine import run_agent
    from tests.agent.test_engine_parallel_reads import (
        _RecordingServer,
        _ScriptedStream,
        _session_factory,
        _turn,
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant, name="Sales", frame=_frame("gmail", department_asks=department_asks)
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
        conn = m.McpConnection(
            tenant_id=tenant,
            department_id=dept.id,
            name="gmail",
            transport="stdio",
            server_url="stdio://gmail",
            connected=True,
            config={"command": "x", "args": []},
            scopes={"read": ["search_mail"], "modify": ["send_mail"]},
        )
        db.add(conn)
        await db.flush()
        anna = await _anna_asks_first(db, tenant, agent.id, "gmail")

    recorder = _RecordingServer(delay_s=0)
    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession",
        _session_factory(recorder, ["search_mail", "send_mail"]),
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn("", ToolCall(id="c1", name="send_mail", arguments={"to": "a@b.c"})),
                _turn("done"),
            ]
        ),
    )
    async with app_session(tenant) as db:
        loaded = await db.get(m.Agent, agent.id)
        loaded_conn = await db.get(m.McpConnection, conn.id)
        assert loaded is not None and loaded_conn is not None
        await run_agent(
            db,
            agent=loaded,
            task_text="send it",
            tenant_id=tenant,
            mcp_conn=loaded_conn,
            originating_operator=ANNA,
        )
    assert not [w for w in recorder.windows if w[0] == "send_mail"]
    _assert_routed(await _held(app_session, tenant), anna, department_asks=department_asks)


# ---------------------------------------------------------------- MCP gateway


@pytest.mark.parametrize("department_asks", [True, False])
async def test_the_gateway_routes_annas_held_call_to_her(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, department_asks: bool
) -> None:
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _task = await _seed(
            db, tenant, frame=_frame("odoo", department_asks=department_asks)
        )
        anna = await _anna_asks_first(db, tenant, agent_id, "odoo")
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "originating_operator": ANNA}

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "res.partner"}},
    )
    assert code == 200, body
    assert not _FakeMcp.calls
    _assert_routed(await _held(app_session, tenant), anna, department_asks=department_asks)


# ----------------------------------------------------- isolated internal API


class _OkSession:
    tools: list[Any] = []

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def __aenter__(self) -> _OkSession:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return "created id=1"


@pytest.mark.parametrize("department_asks", [True, False])
async def test_the_internal_api_routes_annas_held_call_to_her(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, department_asks: bool
) -> None:
    from tests.api.test_internal_agent import _mcp_backed_run, _post_tool

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _OkSession)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _mcp_backed_run(db, tenant)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = _frame("odoo", department_asks=department_asks)
        anna = await _anna_asks_first(db, tenant, agent_id, "odoo")
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "originating_operator": ANNA}

    code, body = await _post_tool(
        tenant, agent_id, run_id, "create_record", {"model": "sale.order", "values": {}}
    )
    assert code == 200, body
    assert body["status"] == "waiting_for_approval", body
    _assert_routed(await _held(app_session, tenant), anna, department_asks=department_asks)
