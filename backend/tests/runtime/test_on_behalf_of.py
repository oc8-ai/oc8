"""Ruling 18: a run that continues a PERSON's work -- a lead's wake-up after a
delegation, a decision follow-up -- carries `on_behalf_of`, so it is that
person's run on everybody's Live timeline (redacted, "All runs" only), never
the agent's own autonomous run. `originating_operator` is unchanged."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import REQUEST_DECISION, _delegate, execute_control_tool
from oc8.agent.decision_followup import carry_out_decision
from oc8.authz.pdp import Decision, Effect
from oc8.chat.live import get_or_create_live_session, is_autonomous_run, live_timeline
from oc8.modelrouter import ToolCall
from oc8.runtime.executor import _maybe_wake_parent
from oc8.runtime.repository import RunRepository
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _Queue:
    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        return None


async def _world(
    db: AsyncSession, tenant: uuid.UUID, lead_ctx: dict[str, Any], source: str
) -> tuple[m.Agent, m.Agent, m.Task, m.AgentRun, m.OrgMember]:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    lead = m.Agent(tenant_id=tenant, department_id=dept.id, name="Lead", is_team_lead=True)
    worker = m.Agent(tenant_id=tenant, department_id=dept.id, name="Worker")
    alice = m.OrgMember(
        tenant_id=tenant, subject="alice", subject_uuid=uuid.uuid4(), display_name="Alice"
    )
    db.add_all([lead, worker, alice])
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=lead.id,
        title="big job",
        state="in_progress",
        delegation_depth=0,
    )
    db.add(task)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant, agent_id=lead.id, task_id=task.id, source=source, context=lead_ctx
    )
    db.add(run)
    await db.flush()
    return lead, worker, task, run, alice


async def _delegate_and_wake(
    db: AsyncSession,
    tenant: uuid.UUID,
    *,
    lead: m.Agent,
    worker: m.Agent,
    task: m.Task,
    run_id: uuid.UUID,
) -> m.AgentRun:
    _out, sub_id = await _delegate(
        db, tenant_id=tenant, agent=lead, task=task, mcp_conn=None, run_id=run_id,
        tc=ToolCall(
            id="d", name="delegate_task",
            arguments={"agent_id": str(worker.id), "task_text": "job"},
        ),
    )  # fmt: skip
    assert sub_id is not None
    sub = await db.get(m.AgentRun, sub_id)
    assert sub is not None
    ctx = sub.context
    wake_id = await _maybe_wake_parent(
        db, repo=RunRepository(db), tenant_id=tenant, parent_task_id=task.id,
        delegation_depth=1, finished_agent_id=worker.id, sub_task_label="job",
        output="done", succeeded=True, mcp_conn=None,
        chat_session_id=ctx.get("chat_session_id"), door=ctx.get("door"),
        followup=ctx.get("followup"), originating_operator=ctx.get("originating_operator"),
        on_behalf_of=ctx.get("on_behalf_of"),
    )  # fmt: skip
    assert wake_id is not None
    wake = await db.get(m.AgentRun, wake_id)
    assert wake is not None
    return wake


async def _on_alices_timeline(
    db: AsyncSession, tenant: uuid.UUID, alice: m.OrgMember, agent: m.Agent
) -> set[uuid.UUID]:
    live = await get_or_create_live_session(
        db, tenant_id=tenant, member_id=alice.id, agent_id=agent.id
    )
    items = await live_timeline(
        db, member=alice, agent=agent, session=live, before=None, limit=50, all_runs=False
    )
    return {i.run.id for i in items if i.run is not None}


async def test_a_manual_runs_delegated_chain_stays_that_persons(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, worker, task, run, alice = await _world(
            db, tenant, {"originating_operator": "bob", "task": "BOB WORDS"}, "manual"
        )
        wake = await _delegate_and_wake(
            db, tenant, lead=lead, worker=worker, task=task, run_id=run.id
        )
        assert wake.source == "delegation"
        assert wake.context["on_behalf_of"] == "bob"
        # Authority is untouched: the wake-up does not act AS bob.
        assert "originating_operator" not in wake.context
        assert not is_autonomous_run(wake)
        # A second hop from the wake-up keeps the person too.
        again = await _delegate_and_wake(
            db, tenant, lead=lead, worker=worker, task=task, run_id=wake.id
        )
        assert again.context["on_behalf_of"] == "bob"
        assert not is_autonomous_run(again)
        assert await _on_alices_timeline(db, tenant, alice, lead) == set()


async def test_the_parent_run_names_the_person_when_the_child_does_not(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _lead, worker, task, _run, _alice = await _world(
            db, tenant, {"originating_operator": "bob"}, "manual"
        )
        wake_id = await _maybe_wake_parent(
            db, repo=RunRepository(db), tenant_id=tenant, parent_task_id=task.id,
            delegation_depth=1, finished_agent_id=worker.id, sub_task_label="job",
            output="done", succeeded=True, mcp_conn=None,
        )  # fmt: skip
        assert wake_id is not None
        wake = await db.get(m.AgentRun, wake_id)
        assert wake is not None and wake.context["on_behalf_of"] == "bob"


async def test_a_scheduled_chain_stays_autonomous(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lead, worker, task, run, alice = await _world(db, tenant, {"task": "nightly"}, "cron")
        wake = await _delegate_and_wake(
            db, tenant, lead=lead, worker=worker, task=task, run_id=run.id
        )
        assert "on_behalf_of" not in wake.context
        assert is_autonomous_run(wake)
        assert wake.id in await _on_alices_timeline(db, tenant, alice, lead)


async def _decide(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    lead_ctx: dict[str, Any],
    source: str,
) -> tuple[m.AgentRun, m.OrgMember, m.Agent]:
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: _Queue())
    async with app_session(tenant) as db:
        lead, _worker, task, run, alice = await _world(db, tenant, lead_ctx, source)
        out = await execute_control_tool(
            db, tenant_id=tenant, agent=lead, task=task,
            tc=ToolCall(
                id="q", name=REQUEST_DECISION.name,
                arguments={"question": "refund?", "context": "customer asked"},
            ),
            decision=Decision(Effect.ALLOW), assigned_skills=[], active_skills=[],
            mcp_conn=None, originating_operator=lead_ctx.get("originating_operator"),
            run_id=run.id,
        )  # fmt: skip
        assert out is not None
        assert not out.output.startswith("ERROR"), out.output
        approval = (
            await db.execute(
                m.ApprovalRequest.__table__.select().where(
                    m.ApprovalRequest.tenant_id == tenant,
                    m.ApprovalRequest.action_type == "decision",
                )
            )
        ).one()
        approval_row = await db.get(m.ApprovalRequest, approval.id)
        assert approval_row is not None
        follow_id = await carry_out_decision(
            db, approval=approval_row, decision="approve", reason="", option=None
        )
    assert follow_id is not None
    async with app_session(tenant) as db:
        follow = await db.get(m.AgentRun, follow_id)
        assert follow is not None
        return follow, alice, lead


async def test_a_decision_follow_up_from_a_members_run_is_that_members(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    follow, alice, lead = await _decide(
        app_session, tenant, monkeypatch, {"originating_operator": "bob"}, "manual"
    )
    assert follow.source == "decision"
    assert follow.context["on_behalf_of"] == "bob"
    assert "originating_operator" not in follow.context
    assert not is_autonomous_run(follow)
    async with app_session(tenant) as db:
        assert await _on_alices_timeline(db, tenant, alice, lead) == set()


async def test_a_decision_follow_up_from_an_autonomous_run_stays_autonomous(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    follow, _alice, _lead = await _decide(app_session, tenant, monkeypatch, {}, "cron")
    assert "on_behalf_of" not in follow.context
    assert is_autonomous_run(follow)
