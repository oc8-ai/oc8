"""A tool_send card raised without `payload.run_id` on a Copilot task that has
several runs must still resume the parked leg, not 500 the decision.

Regression: the fallback `scalar_one_or_none()` raised MultipleResultsFound
(HTTP 500 with an empty message) from the second turn of a Copilot conversation
on, so the person could never push their decision.
"""

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.runtime.approval_resume import resolve_tool_approval
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _task_with_two_runs(
    db: Any, tenant: uuid.UUID, *, parked: bool
) -> tuple[m.Task, m.AgentRun, m.AgentRun]:
    dept = m.Department(tenant_id=tenant, name="Sprzedaż", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Sales Specialist",
        status="running", narrowing={}, definition={}, presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t",
                  state="waiting_for_approval")
    db.add(task)
    await db.flush()
    first = m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, task_id=task.id,
        state=RunState.DONE.value, context={"task": "turn 1"},
    )
    db.add(first)
    await db.flush()
    second = m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, task_id=task.id,
        state=(RunState.WAITING_FOR_APPROVAL.value if parked else RunState.RUNNING.value),
        context={"task": "turn 2"},
    )
    db.add(second)
    await db.flush()
    return task, first, second


async def _unlinked_card(
    db: Any, tenant: uuid.UUID, agent: m.Agent, task: m.Task
) -> m.ApprovalRequest:
    ar = m.ApprovalRequest(
        tenant_id=tenant, agent_id=agent.id, task_id=task.id,
        action_type="tool_send", status="pending",
        payload={"tool": "create_records", "arguments": {"model": "crm.lead"}},
    )
    db.add(ar)
    await db.flush()
    return ar


async def test_a_second_run_on_the_task_does_not_break_the_decision(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task, _first, second = await _task_with_two_runs(db, tenant, parked=True)
        agent = await db.get(m.Agent, second.agent_id)
        assert agent is not None
        approval = await _unlinked_card(db, tenant, agent, task)
        resumed = await resolve_tool_approval(db, approval=approval, decision="approve")
        assert resumed is not None and resumed.id == second.id
        assert resumed.state == RunState.QUEUED.value


async def test_the_newest_run_is_chosen_when_none_is_parked(
    app_session: AppSessionFactory,
) -> None:
    """No parked leg: the decision is recorded (no 500, no MultipleResultsFound)
    and nothing is re-queued -- a live runtime picks it up on its next attempt."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task, _first, second = await _task_with_two_runs(db, tenant, parked=False)
        agent = await db.get(m.Agent, second.agent_id)
        assert agent is not None
        approval = await _unlinked_card(db, tenant, agent, task)
        resumed = await resolve_tool_approval(db, approval=approval, decision="reject")
        assert resumed is None
        stored = await db.get(m.AgentRun, second.id, populate_existing=True)
        assert stored is not None
        entry = stored.context["resolved_tool_approvals"][-1]
        assert entry["decision"] == "reject" and entry["tool"] == "create_records"
