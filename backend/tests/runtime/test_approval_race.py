"""A decision that lands while the run is still parking must not be lost.

The gateway writes its park marker, the adapter notices, the container is torn
down, and only then does the executor transition the run to WAITING_FOR_APPROVAL.
An operator deciding inside that window recorded the decision against a run that
was still RUNNING -- so nothing re-queued it, and the run waited forever for a
decision that had already been made.

`resolved_tool_approvals` accumulates across every approval cycle a run goes
through, so a run parking a SECOND time (for a different call) already carries
the first call's decided entry. Matching on mere presence of that list would
re-queue the run before anyone decided the new call -- see
test_a_second_park_with_only_a_stale_decision_stays_parked below.

The race has two halves, closed on two different sides:

  * `requeue_if_already_decided` (executor.py), tested above, catches a decision
    that was ALREADY COMMITTED by the time the run's WAITING_FOR_APPROVAL
    transition runs.
  * `resolve_tool_approval`'s re-read (approval_resume.py), tested below, catches
    a decision whose own write blocks on that very transition's row lock and
    lands the instant it releases -- narrower (milliseconds, not the seconds of
    a poll-plus-teardown), but the same stranding if left open.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.runtime.approval_resume import call_signature, resolve_tool_approval
from oc8.runtime.executor import requeue_if_already_decided
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _parked_run(
    db: Any,
    tenant: uuid.UUID,
    *,
    context: dict[str, Any],
    current_hold: tuple[str, dict[str, Any]] | None,
    hold_status: str = "pending",
) -> m.AgentRun:
    """A run sitting in WAITING_FOR_APPROVAL, plus (optionally) the
    ApprovalRequest describing the call it is CURRENTLY holding -- i.e. the
    gateway's park marker for this park, as opposed to any earlier, already
    resolved park recorded in `resolved_tool_approvals`."""
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", status="running",
        narrowing={}, definition={}, presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t",
                  state="waiting_for_approval")
    db.add(task)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, task_id=task.id,
        state=RunState.WAITING_FOR_APPROVAL.value, context=context,
    )
    db.add(run)
    await db.flush()
    if current_hold is not None:
        tool, arguments = current_hold
        ar = m.ApprovalRequest(
            tenant_id=tenant, agent_id=agent.id, task_id=task.id,
            action_type="tool_send", status=hold_status,
            payload={"tool": tool, "arguments": arguments},
        )
        db.add(ar)
        await db.flush()
    return run


async def test_a_decision_recorded_during_the_park_re_queues_the_run(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    tool, args = "create_record", {"model": "crm.lead"}
    async with app_session(tenant) as db:
        context = {
            "task": "x",
            "resolved_tool_approvals": [
                {"sig": call_signature(tool, args), "tool": tool, "arguments": args,
                 "decision": "approve"}
            ],
        }
        # The decision was already recorded (resolved_tool_approvals above); the
        # ApprovalRequest it resolved is the run's current hold, now "approved".
        run = await _parked_run(
            db, tenant, context=context, current_hold=(tool, args), hold_status="approved"
        )

        requeued = await requeue_if_already_decided(db, run=run)

        assert requeued is True
        assert run.state == RunState.QUEUED.value


async def test_a_run_still_waiting_for_a_human_stays_parked(
    app_session: AppSessionFactory,
) -> None:
    """The ordinary case, and the one that must not regress: no decision yet, so
    the run waits."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _parked_run(
            db, tenant, context={"task": "x"},
            current_hold=("create_record", {"model": "crm.lead"}), hold_status="pending",
        )

        requeued = await requeue_if_already_decided(db, run=run)

        assert requeued is False
        assert run.state == RunState.WAITING_FOR_APPROVAL.value


async def test_a_second_park_with_only_a_stale_decision_stays_parked(
    app_session: AppSessionFactory,
) -> None:
    """A run that was resumed once (its context already carries the FIRST
    decision) and has now parked again for a SECOND, still-undecided call must
    not be re-queued off the leftover entry from the first decision."""
    tenant = uuid.uuid4()
    first_tool, first_args = "create_record", {"model": "crm.lead"}
    second_tool, second_args = "update_record", {"model": "crm.lead", "id": 7}
    async with app_session(tenant) as db:
        context = {
            "task": "x",
            "resolved_tool_approvals": [
                {"sig": call_signature(first_tool, first_args), "tool": first_tool,
                 "arguments": first_args, "decision": "approve"}
            ],
        }
        run = await _parked_run(
            db, tenant, context=context,
            current_hold=(second_tool, second_args), hold_status="pending",
        )

        requeued = await requeue_if_already_decided(db, run=run)

        assert requeued is False
        assert run.state == RunState.WAITING_FOR_APPROVAL.value


async def test_a_decision_that_blocks_on_the_transitioning_row_still_requeues(
    app_session: AppSessionFactory,
) -> None:
    """The residual half of the parking race, reproduced with two genuinely
    concurrent transactions rather than a scripted stand-in.

    Real sequence this stands for: the executor's `repo.transition(run,
    WAITING_FOR_APPROVAL)` has issued its UPDATE and is still holding the row
    lock (its outer `tenant_session` has not committed yet) when an operator's
    decision arrives. `resolve_tool_approval` loads the run -- still RUNNING,
    because the transition hasn't committed -- appends the decision, and then
    its own UPDATE (recording `resolved_tool_approvals`) blocks on the
    executor's lock. Only once the executor commits does that UPDATE proceed;
    the state check must then re-read rather than trust the RUNNING snapshot
    taken before it blocked.

    Session B below plays the executor: it flushes the RUNNING ->
    WAITING_FOR_APPROVAL transition (issuing the UPDATE, taking the lock) but
    deliberately does not commit yet. `resolve_tool_approval` is driven
    concurrently as session A; the test asserts it is genuinely still blocked
    before B commits, then that it resumes correctly once B does.
    """
    tenant = uuid.uuid4()
    tool, args = "create_record", {"model": "crm.lead"}

    setup_cm = app_session(tenant)
    setup_db = await setup_cm.__aenter__()
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    setup_db.add(dept)
    await setup_db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", status="running",
        narrowing={}, definition={}, presentation={},
    )
    setup_db.add(agent)
    await setup_db.flush()
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t", state="in_progress")
    setup_db.add(task)
    await setup_db.flush()
    run = m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, task_id=task.id,
        state=RunState.RUNNING.value, context={"task": "x"},
    )
    setup_db.add(run)
    await setup_db.flush()
    run_id, task_id, agent_id = run.id, task.id, agent.id
    await setup_cm.__aexit__(None, None, None)

    # Session B: the executor's transition, flushed (UPDATE issued, row lock
    # held) but its outer transaction is deliberately left open.
    executor_cm = app_session(tenant)
    executor_db = await executor_cm.__aenter__()
    run_b = await executor_db.get(m.AgentRun, run_id)
    assert run_b is not None
    run_b.state = RunState.WAITING_FOR_APPROVAL.value
    await executor_db.flush()

    async def _decide() -> m.AgentRun | None:
        decide_cm = app_session(tenant)
        decide_db = await decide_cm.__aenter__()
        ar = m.ApprovalRequest(
            tenant_id=tenant, agent_id=agent_id, task_id=task_id,
            action_type="tool_send", status="pending",
            payload={"tool": tool, "arguments": args},
        )
        decide_db.add(ar)
        await decide_db.flush()
        result = await resolve_tool_approval(decide_db, approval=ar, decision="approve")
        await decide_cm.__aexit__(None, None, None)
        return result

    decide_task = asyncio.create_task(_decide())
    # Let session A run until it genuinely blocks on B's uncommitted row lock.
    await asyncio.sleep(0.3)
    assert not decide_task.done(), (
        "resolve_tool_approval finished without contending for the row lock -- "
        "this test is not reproducing the race it claims to"
    )

    # B commits, releasing the lock; A's blocked UPDATE -- and its re-read --
    # can now proceed and must see the committed WAITING_FOR_APPROVAL state.
    await executor_cm.__aexit__(None, None, None)

    resumed = await asyncio.wait_for(decide_task, timeout=5)
    assert resumed is not None
    assert resumed.id == run_id
    assert resumed.state == RunState.QUEUED.value


async def test_no_resolved_approvals_never_queries_or_requeues(
    app_session: AppSessionFactory,
) -> None:
    """Defensive baseline: nothing recorded at all -> no re-queue, regardless of
    whether a hold is present."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _parked_run(db, tenant, context={"task": "x"}, current_hold=None)

        requeued = await requeue_if_already_decided(db, run=run)

        assert requeued is False
        assert run.state == RunState.WAITING_FOR_APPROVAL.value


async def test_a_decided_card_re_queues_only_on_its_own_decision(
    app_session: AppSessionFactory,
) -> None:
    """An entry names the card it decided (`approval_id`): one that decided
    an earlier card of the same call does not re-queue a park on another."""
    tenant = uuid.uuid4()
    tool, args = "create_record", {"model": "crm.lead"}
    async with app_session(tenant) as db:
        entry = {
            "sig": call_signature(tool, args),
            "tool": tool,
            "arguments": args,
            "decision": "approve",
            "approval_id": str(uuid.uuid4()),
        }
        run = await _parked_run(
            db,
            tenant,
            context={"task": "x", "resolved_tool_approvals": [entry]},
            current_hold=(tool, args),
            hold_status="approved",
        )

        assert await requeue_if_already_decided(db, run=run) is False
        assert run.state == RunState.WAITING_FOR_APPROVAL.value
