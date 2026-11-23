"""resolve_tool_approval records the operator's verdict on the run and re-queues
it; the resume instruction + pre-decided map steer/execute exactly the decided
calls."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from oc8 import models as m
from oc8.runtime.approval_resume import (
    call_signature,
    pre_decided_map,
    resolve_tool_approval,
    resume_instruction,
)
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _suspended_run(session: AppSessionFactory, tenant: uuid.UUID):
    async with session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="D", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="A", status="waiting_for_approval",
            narrowing={}, definition={},
        )
        db.add(agent)
        await db.flush()
        task = m.Task(tenant_id=tenant, department_id=dept.id, title="t",
                      state="waiting_for_approval")
        db.add(task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, task_id=task.id,
            state=RunState.WAITING_FOR_APPROVAL.value, context={"task": "original"},
        )
        db.add(run)
        await db.flush()
        return run.id, task.id


async def test_approve_records_verdict_and_requeues(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    run_id, task_id = await _suspended_run(app_session, tenant)
    args = {"model": "sale.order", "values": {"x": 1}}
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant, agent_id=uuid.uuid4(), task_id=task_id,
            action_type="tool_send", status="approved",
            payload={"tool": "create_record", "arguments": args},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision="approve")
        assert run is not None and run.id == run_id
        assert run.state == RunState.QUEUED.value
        entries = run.context["resolved_tool_approvals"]
        assert entries[0]["decision"] == "approve"
        assert entries[0]["sig"] == call_signature("create_record", args)


async def test_no_resume_when_run_not_waiting(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    _run_id, task_id = await _suspended_run(app_session, tenant)
    async with app_session(tenant) as db:
        run = (
            await db.execute(
                m.AgentRun.__table__.select().where(m.AgentRun.task_id == task_id)
            )
        ).first()
        # Flip the run out of waiting so resume must decline.
        r = await db.get(m.AgentRun, run.id)
        r.state = RunState.DONE.value
        await db.flush()
        ar = m.ApprovalRequest(
            tenant_id=tenant, agent_id=uuid.uuid4(), task_id=task_id,
            action_type="tool_send", status="approved",
            payload={"tool": "create_record", "arguments": {}},
        )
        db.add(ar)
        await db.flush()
        assert await resolve_tool_approval(db, approval=ar, decision="approve") is None


async def _second_turn(
    session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A chat's two turns on ONE task: turn 1 finished, turn 2 parked on a
    held write. Returns (turn 1 run, turn 2 run, task)."""
    turn2_id, task_id = await _suspended_run(session, tenant)
    async with session(tenant) as db:
        turn2 = await db.get(m.AgentRun, turn2_id)
        assert turn2 is not None
        turn1 = m.AgentRun(
            tenant_id=tenant,
            agent_id=turn2.agent_id,
            task_id=task_id,
            state=RunState.DONE.value,
            context={"task": "turn 1"},
            created_at=turn2.created_at - dt.timedelta(minutes=1),
        )
        db.add(turn1)
        await db.flush()
        return turn1.id, turn2_id, task_id


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_a_second_turn_approval_resumes_the_run_that_raised_it(
    app_session: AppSessionFactory, decision: str
) -> None:
    """E2E F6: both turns of an agent chat share one task, so a lookup by task
    matched two runs (500 MultipleResultsFound) and the run parked forever."""
    tenant = uuid.uuid4()
    _turn1, turn2, task_id = await _second_turn(app_session, tenant)
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="pending",
            payload={"tool": "mail_send", "arguments": {"to": "x"}, "run_id": str(turn2)},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision=decision)
        assert run is not None and run.id == turn2
        assert run.state == RunState.QUEUED.value
        assert run.context["resolved_tool_approvals"][0]["decision"] == decision


async def test_an_approval_without_run_id_resumes_the_task_run_that_raised_it(
    app_session: AppSessionFactory,
) -> None:
    """A card raised before its raiser named the run (still pending across the
    upgrade): the run that raised it is the task's latest run started before
    it, never "any run of the task"."""
    tenant = uuid.uuid4()
    _turn1, turn2, task_id = await _second_turn(app_session, tenant)
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="pending",
            payload={"tool": "mail_send", "arguments": {}},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision="approve")
        assert run is not None and run.id == turn2


def test_resume_instruction_lists_approved_and_rejected() -> None:
    entries = [
        {"sig": "a", "tool": "create_record", "arguments": {"v": 1}, "decision": "approve"},
        {"sig": "b", "tool": "update_record", "arguments": {"v": 2}, "decision": "reject"},
    ]
    text = resume_instruction(entries)
    assert "FREIGEGEBEN" in text and "create_record" in text
    assert "ABGELEHNT" in text and "update_record" in text
    assert pre_decided_map(entries) == {"a": "approve", "b": "reject"}
