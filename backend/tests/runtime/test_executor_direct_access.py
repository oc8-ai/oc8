"""The executor's side of a Copilot direct-access park and resume.

* A run parked on `offer_direct_access` whose offer was already decided is
  woken at once -- and a stale, decided held write of an earlier leg never
  wakes a run that is now waiting on an undecided offer.
* A resume after a held write AND an answered offer tells the run both.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.constants import ACME_TENANT_ID
from oc8.runtime.approval_resume import call_signature
from oc8.runtime.executor import execute_run, requeue_if_already_decided
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import append_resolved_approval
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

WRITE = ("update_record", {"id": 7})


async def _parked(
    db: Any,
    tenant: uuid.UUID,
    *,
    context: dict[str, Any],
    holds: list[tuple[str, str, dict[str, Any]]],
) -> m.AgentRun:
    """A run in WAITING_FOR_APPROVAL and its task's approvals, oldest first:
    (action_type, status, payload)."""
    dept = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Copilot")
    db.add(agent)
    await db.flush()
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t", state="waiting_for_approval")
    db.add(task)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        state=RunState.WAITING_FOR_APPROVAL.value,
        context=context,
    )
    db.add(run)
    await db.flush()
    start = dt.datetime.now(dt.UTC)
    for i, (action_type, status, payload) in enumerate(holds):
        db.add(
            m.ApprovalRequest(
                tenant_id=tenant,
                agent_id=agent.id,
                task_id=task.id,
                action_type=action_type,
                status=status,
                title="t",
                payload={"run_id": str(run.id), **payload}
                if action_type == "direct_access"
                else payload,
                # Each leg raised its hold in its own transaction.
                created_at=start + dt.timedelta(seconds=i),
            )
        )
        await db.flush()
    return run


def _offer(status: str) -> tuple[str, str, dict[str, Any]]:
    return (
        "direct_access",
        status,
        {"tool": "offer_direct_access", "arguments": {"connection": "things"}},
    )


def _write(status: str) -> tuple[str, str, dict[str, Any]]:
    return ("tool_send", status, {"tool": WRITE[0], "arguments": WRITE[1]})


def _decided_write() -> dict[str, Any]:
    return {
        "sig": call_signature(*WRITE),
        "tool": WRITE[0],
        "arguments": WRITE[1],
        "decision": "approve",
    }


async def test_an_offer_decided_before_the_park_wakes_the_run(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _parked(
            db,
            tenant,
            context={
                "task": "x",
                "resolved_direct_access": {"tool_key": "things", "decision": "approve"},
            },
            holds=[_offer("approved")],
        )
        assert await requeue_if_already_decided(db, run=run) is True
        assert run.state == RunState.QUEUED.value


async def test_an_undecided_offer_stays_parked_despite_a_stale_decided_write(
    app_session: AppSessionFactory,
) -> None:
    """The earlier leg's held write is decided; THIS park holds the offer."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _parked(
            db,
            tenant,
            context={"task": "x", "resolved_tool_approvals": [_decided_write()]},
            holds=[_write("approved"), _offer("pending")],
        )
        assert await requeue_if_already_decided(db, run=run) is False
        assert run.state == RunState.WAITING_FOR_APPROVAL.value


async def test_another_runs_decided_offer_does_not_wake_this_run(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _parked(
            db,
            tenant,
            context={
                "task": "x",
                "resolved_direct_access": {"tool_key": "things", "decision": "approve"},
            },
            holds=[],
        )
        assert run.task_id is not None
        db.add(
            m.ApprovalRequest(
                tenant_id=tenant,
                agent_id=run.agent_id,
                task_id=run.task_id,
                action_type="direct_access",
                status="approved",
                title="t",
                payload={"run_id": str(uuid.uuid4())},
            )
        )
        await db.flush()
        assert await requeue_if_already_decided(db, run=run) is False


class _FnRuntime:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def execute(self, db: Any, **kw: Any) -> RunResult:
        result: RunResult = await self._fn(db, **kw)
        return result


OFFER_NOTE = "The person approved reading Things (things)"
ANSWER = {"tool_key": "things", "label": "Things", "decision": "approve"}


async def _queued_run(app_session: AppSessionFactory, context: dict[str, Any]) -> uuid.UUID:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        created = await RunRepository(s).create(
            tenant_id=tenant, agent_id=agent.id, context={"task": "find the quotation", **context}
        )
        return created.id


async def _resume(run_id: uuid.UUID, status: str = "done") -> str:
    """One leg; returns the task text the runtime was handed."""
    seen: dict[str, Any] = {}

    async def capturing(db: Any, **kw: Any) -> RunResult:
        seen.update(kw)
        return RunResult(task_id=uuid.uuid4(), agent_id=kw["agent"].id, status=status, output="ok")

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(capturing),
    )
    return str(seen["task_text"])


async def _requeue_with(
    app_session: AppSessionFactory, run_id: uuid.UUID, entry: dict[str, Any]
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        await append_resolved_approval(s, run, entry)
        await RunRepository(s).transition(run, RunState.QUEUED)


async def test_each_leg_is_told_only_the_answer_that_woke_it(
    app_session: AppSessionFactory,
) -> None:
    """offer approved -> the leg carries on with the request; then a held write
    approved -> the next leg gets the write's instruction alone, not the old
    offer note (which would tell it to carry on)."""
    run_id = await _queued_run(app_session, {"resolved_direct_access": ANSWER})
    first = await _resume(run_id, status="waiting_for_approval")
    assert first.startswith("find the quotation") and OFFER_NOTE in first

    await _requeue_with(app_session, run_id, _decided_write())
    second = await _resume(run_id)
    assert "FREIGEGEBEN" in second and "update_record" in second
    assert OFFER_NOTE not in second
    async with app_session(uuid.UUID(str(ACME_TENANT_ID))) as s:
        run = await s.get(m.AgentRun, run_id)
        assert run is not None
        assert "resolved_direct_access" not in run.context


async def test_an_offer_answered_after_an_earlier_held_write_wakes_its_leg_with_the_answer(
    app_session: AppSessionFactory,
) -> None:
    """The held write was decided in an earlier leg; THIS leg was woken by the
    offer's answer, so the run carries on with the request."""
    run_id = await _queued_run(
        app_session,
        {"resolved_tool_approvals": [_decided_write()], "resolved_direct_access": ANSWER},
    )
    text = await _resume(run_id)
    assert OFFER_NOTE in text
    assert "FREIGEGEBEN" not in text


@pytest.mark.parametrize("status", ["done", "waiting_for_approval"])
async def test_a_mode_the_leg_entered_reaches_the_chat_at_the_legs_end(
    app_session: AppSessionFactory, status: str
) -> None:
    """`enter_agent_mode` only records the mode on the run: the chat's row is
    switched by the executor at the end of the leg -- whether it finishes or
    parks -- under `chat.tree`'s lock order, not during the leg."""
    from oc8.runtime.run_context import merge_context

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Copilot")
        s.add(agent)
        await s.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
        s.add(session)
        await s.flush()
        session_id = session.id
        run_id = (
            await RunRepository(s).create(
                tenant_id=tenant,
                agent_id=agent.id,
                source="chat",
                context={"task": "build me a checker", "chat_session_id": str(session.id)},
            )
        ).id
    mode_during_leg: list[str | None] = []

    async def entering(db: Any, **kw: Any) -> RunResult:
        run = await db.get(m.AgentRun, kw["run_id"])
        await merge_context(db, run, {"entered_mode": "agent"})
        row = await db.get(m.ChatSession, session_id)
        mode_during_leg.append(row.mode)
        return RunResult(task_id=uuid.uuid4(), agent_id=kw["agent"].id, status=status, output="ok")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(entering),
    )
    assert mode_during_leg == [None]
    async with app_session(tenant) as s:
        row = await s.get(m.ChatSession, session_id)
        run = await s.get(m.AgentRun, run_id)
        assert row is not None and run is not None
        assert row.mode == "agent"
        assert run.context.get("entered_mode") is None
