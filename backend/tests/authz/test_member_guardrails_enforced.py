"""The acting member's guardrails bite on every tool path (D3a).

The acting member is the run's `originating_operator` -- the person who
posted the turn, carried down delegations -- never the session owner. Anna has
a guardrail on the agent that switches its modifying right off; Ben has none.
Every enforcement path must deny Anna's modifying call and allow Ben's.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.member_guardrails import member_term_for_run
from oc8.authz.pdp import effective_tool_policies
from oc8.modelrouter import ToolCall
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

ANNA = "anna@x.de"
BEN = "ben@x.de"
ADMIN = "admin@x.de"


def _rights(key: str, *, modify: bool) -> dict[str, Any]:
    return {"tools": {key: {"enabled": True, "read": True, "modify": modify}}}


async def _members(db: AsyncSession, tenant: uuid.UUID, agent_id: uuid.UUID, key: str) -> None:
    """Anna (guarded: no modify on `key`), Ben and an admin (no guardrail)."""
    anna = m.OrgMember(tenant_id=tenant, subject=ANNA, subject_uuid=uuid.uuid4())
    ben = m.OrgMember(tenant_id=tenant, subject=BEN, subject_uuid=uuid.uuid4())
    admin = m.OrgMember(tenant_id=tenant, subject=ADMIN, subject_uuid=uuid.uuid4())
    db.add_all([anna, ben, admin])
    await db.flush()
    db.add(
        m.MemberGuardrail(
            tenant_id=tenant,
            member_id=anna.id,
            agent_id=agent_id,
            policy=_rights(key, modify=False),
        )
    )
    await db.flush()


async def _seed_loader(app_session: AppSessionFactory) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame=_rights("gmail", modify=True))
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Sales agent",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(agent)
        await db.flush()
        await _members(db, tenant, agent.id, "gmail")
        return tenant, agent.id


# ------------------------------------------------------------------- loader


async def test_term_follows_the_originating_operator(app_session: AppSessionFactory) -> None:
    tenant, agent_id = await _seed_loader(app_session)
    async with app_session(tenant) as db:
        member, term = await member_term_for_run(
            db, tenant_id=tenant, agent_id=agent_id, context={"originating_operator": ANNA}
        )
        assert member is not None and member.subject == ANNA
        assert term == _rights("gmail", modify=False)
        ben, other = await member_term_for_run(
            db, tenant_id=tenant, agent_id=agent_id, context={"originating_operator": BEN}
        )
        assert ben is not None and other is None


async def test_no_operator_no_term(app_session: AppSessionFactory) -> None:
    tenant, agent_id = await _seed_loader(app_session)
    async with app_session(tenant) as db:
        member, term = await member_term_for_run(
            db, tenant_id=tenant, agent_id=agent_id, context={}
        )
        assert member is None and term is None
        member, term = await member_term_for_run(
            db, tenant_id=tenant, agent_id=agent_id, context=None
        )
        assert member is None and term is None


async def test_a_deleted_member_has_no_term(app_session: AppSessionFactory) -> None:
    import datetime as dt

    from sqlalchemy import select

    tenant, agent_id = await _seed_loader(app_session)
    async with app_session(tenant) as db:
        anna = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == ANNA))
        ).scalar_one()
        anna.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        member, term = await member_term_for_run(
            db, tenant_id=tenant, agent_id=agent_id, context={"originating_operator": ANNA}
        )
        assert member is None and term is None


# ------------------------------------------------------------ in-process engine


async def test_the_engine_denies_annas_modifying_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
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
        dept = m.Department(tenant_id=tenant, name="Sales", frame=_rights("gmail", modify=True))
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
        await _members(db, tenant, agent.id, "gmail")

    # One recorder for both runs: the session pool may hand the second run the
    # first run's session.
    recorder = _RecordingServer(delay_s=0)
    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession",
        _session_factory(recorder, ["search_mail", "send_mail"]),
    )
    seen: dict[str, int] = {}
    for operator in (ANNA, BEN):
        before = len(recorder.windows)
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
                originating_operator=operator,
            )
        seen[operator] = len([w for w in recorder.windows[before:] if w[0] == "send_mail"])

    assert seen == {ANNA: 0, BEN: 1}


# ---------------------------------------------------------------- MCP gateway


async def _gateway_run(
    app_session: AppSessionFactory, operator: str | None, **extra_ctx: Any
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from tests.api.test_mcp_gateway import _seed

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _task = await _seed(db, tenant, frame=_rights("odoo", modify=True))
        await _members(db, tenant, agent_id, "odoo")
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        ctx: dict[str, Any] = {**run.context, **extra_ctx}
        if operator is not None:
            ctx["originating_operator"] = operator
        run.context = ctx
    return tenant, agent_id, run_id


@pytest.fixture
def _fake_mcp(monkeypatch: pytest.MonkeyPatch) -> Any:
    from oc8.agent import mcp_pool
    from tests.api.test_mcp_gateway import _FakeMcp

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    return mcp_pool


@pytest.fixture(autouse=True)
async def _no_pooled_sessions() -> Any:
    from oc8.agent import mcp_pool

    await mcp_pool.close_all()
    yield
    await mcp_pool.close_all()


@pytest.mark.parametrize(("operator", "denied"), [(ANNA, True), (BEN, False), (None, False)])
async def test_gateway_tools_call_follows_the_acting_member(
    app_session: AppSessionFactory, _fake_mcp: Any, operator: str | None, denied: bool
) -> None:
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _token

    tenant, agent_id, run_id = await _gateway_run(app_session, operator)
    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "res.partner"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is denied, body
    assert bool(_FakeMcp.calls) is not denied


@pytest.mark.parametrize(("operator", "modify_listed"), [(ANNA, False), (BEN, True)])
async def test_gateway_tools_list_omits_annas_modify_only_tools(
    app_session: AppSessionFactory, _fake_mcp: Any, operator: str, modify_listed: bool
) -> None:
    from tests.api.test_mcp_gateway import _rpc, _token

    tenant, agent_id, run_id = await _gateway_run(app_session, operator)
    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert code == 200, body
    names = {t["name"] for t in body["result"]["tools"]}
    assert "search_records" in names
    assert ("create_record" in names) is modify_listed
    assert ("send_email" in names) is modify_listed


async def test_an_admin_posting_into_annas_session_acts_under_the_admins_term(
    app_session: AppSessionFactory, _fake_mcp: Any
) -> None:
    """Review Focus 1: the acting member is the run's originating operator, not
    the session owner. Anna's guardrail must not follow her session."""
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _token

    tenant, agent_id, run_id = await _gateway_run(app_session, ADMIN)
    async with app_session(tenant) as db:
        anna = await _subject(db, ANNA)
        session = m.ChatSession(tenant_id=tenant, agent_id=agent_id, member_id=anna.id)
        db.add(session)
        await db.flush()
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "chat_session_id": str(session.id)}

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "res.partner"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is False, body
    assert _FakeMcp.calls


async def _subject(db: AsyncSession, subject: str) -> m.OrgMember:
    from sqlalchemy import select

    return (
        await db.execute(select(m.OrgMember).where(m.OrgMember.subject == subject))
    ).scalar_one()


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


@pytest.mark.parametrize(("operator", "denied"), [(ANNA, True), (BEN, False)])
async def test_internal_tool_dispatch_follows_the_acting_member(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    operator: str,
    denied: bool,
) -> None:
    from tests.api.test_internal_agent import _mcp_backed_run, _post_tool

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _OkSession)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _mcp_backed_run(db, tenant)
        await _members(db, tenant, agent_id, "odoo")
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "originating_operator": operator}

    code, body = await _post_tool(
        tenant, agent_id, run_id, "create_record", {"model": "sale.order", "values": {}}
    )
    assert code == 200, body
    assert (body["status"] == "denied") is denied, body


# ------------------------------------------------------------------ delegation


async def test_a_run_delegated_by_annas_run_carries_annas_term(
    app_session: AppSessionFactory,
) -> None:
    """Data-level: `delegate_task` copies `originating_operator` into the
    sub-run, and the sub-run's context resolves to Anna's term on the target
    agent -- which every path above then enforces."""
    from oc8.agent.control_tools import _delegate
    from oc8.agent.engine import open_run_task

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        frame = _rights("gmail", modify=True)
        dept = m.Department(tenant_id=tenant, name="Sales", frame=frame)
        db.add(dept)
        await db.flush()
        lead = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Lead", status="running",
            narrowing={}, definition={}, presentation={}, is_team_lead=True,
        )  # fmt: skip
        worker = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Worker", status="running",
            narrowing={}, definition={}, presentation={},
        )  # fmt: skip
        db.add_all([lead, worker])
        await db.flush()
        await _members(db, tenant, worker.id, "gmail")
        task = await open_run_task(db, agent=lead, task_text="go", tenant_id=tenant)
        run = m.AgentRun(
            tenant_id=tenant, agent_id=lead.id, task_id=task.id, state="running",
            context={"task": "go", "originating_operator": ANNA},
        )  # fmt: skip
        db.add(run)
        await db.flush()
        _out, sub_id = await _delegate(
            db, tenant_id=tenant, agent=lead, task=task, mcp_conn=None, run_id=run.id,
            tc=ToolCall(
                id="d", name="delegate_task",
                arguments={"agent_id": str(worker.id), "task_text": "send it"},
            ),
        )  # fmt: skip
        assert sub_id is not None
        sub = await db.get(m.AgentRun, sub_id)
        assert sub is not None and sub.context["originating_operator"] == ANNA
        member, term = await member_term_for_run(
            db, tenant_id=tenant, agent_id=worker.id, context=sub.context
        )
        assert member is not None and member.subject == ANNA
        assert effective_tool_policies(frame, {}, term)["gmail"].modify is False


# ------------------------------------------- messenger turns: the session owner


async def _annas_session(db: AsyncSession, tenant: uuid.UUID, agent_id: uuid.UUID) -> uuid.UUID:
    anna = await _subject(db, ANNA)
    session = m.ChatSession(tenant_id=tenant, agent_id=agent_id, member_id=anna.id)
    db.add(session)
    await db.flush()
    return session.id


async def test_without_an_operator_the_session_owner_acts(
    app_session: AppSessionFactory,
) -> None:
    tenant, agent_id = await _seed_loader(app_session)
    async with app_session(tenant) as db:
        session_id = await _annas_session(db, tenant, agent_id)
        member, term = await member_term_for_run(
            db, tenant_id=tenant, agent_id=agent_id, context={"chat_session_id": str(session_id)}
        )
        assert member is not None and member.subject == ANNA
        assert term == _rights("gmail", modify=False)


@pytest.mark.parametrize("which", ["other_tenant", "unknown", "garbage"])
async def test_a_foreign_or_unknown_session_gives_no_term(
    app_session: AppSessionFactory, which: str
) -> None:
    tenant, agent_id = await _seed_loader(app_session)
    other_tenant, other_agent = await _seed_loader(app_session)
    async with app_session(other_tenant) as db:
        foreign = await _annas_session(db, other_tenant, other_agent)
    session_ref = {
        "other_tenant": str(foreign),
        "unknown": str(uuid.uuid4()),
        "garbage": "not-a-uuid",
    }[which]
    async with app_session(tenant) as db:
        member, term = await member_term_for_run(
            db, tenant_id=tenant, agent_id=agent_id, context={"chat_session_id": session_ref}
        )
        assert member is None and term is None


async def test_a_messenger_turn_in_annas_session_is_denied_the_modifying_call(
    app_session: AppSessionFactory, _fake_mcp: Any
) -> None:
    """Channel dispatch starts the run with no originating operator but with the
    bound member's chat session; her guardrails must still bite."""
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _token

    tenant, agent_id, run_id = await _gateway_run(app_session, None)
    async with app_session(tenant) as db:
        session_id = await _annas_session(db, tenant, agent_id)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {
            **run.context,
            "chat_session_id": str(session_id),
            "chat_channel": "telegram",
            "chat_channel_external_id": "12345",
        }

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "res.partner"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is True, body
    assert _FakeMcp.calls == []
