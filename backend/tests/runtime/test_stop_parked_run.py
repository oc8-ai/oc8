"""Stopping a run that is parked on an approval (E2E F6).

A parked run is not looping, so a cancel row alone was only honoured when
somebody decided the card -- and a chat's second-turn card could not be
decided at all. Stop now closes the run's open card and wakes the run, so the
worker's before-start check ends it."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append(run_id)


def _h(tenant: uuid.UUID, subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _parked_second_turn(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """(turn-2 run, its approval, an older turn's still-pending card, agent, task)."""
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="A", status="waiting_for_approval"
        )
        db.add(agent)
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept.id,
            assigned_agent_id=agent.id,
            title="chat",
            state="waiting_for_approval",
        )
        db.add(task)
        await db.flush()
        ctx = {"originating_operator": "alice"}
        turn1 = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            task_id=task.id,
            source="chat",
            state=RunState.DONE.value,
            context=ctx,
        )
        db.add(turn1)
        await db.flush()
        turn2 = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            task_id=task.id,
            source="chat",
            state=RunState.WAITING_FOR_APPROVAL.value,
            context=ctx,
        )
        db.add(turn2)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            department_id=dept.id,
            task_id=task.id,
            action_type="tool_send",
            status="pending",
            title="A wants to call mail_send",
            payload={"tool": "mail_send", "arguments": {}, "run_id": str(turn2.id)},
        )
        # Another run's card on the same task is not this stop's to close.
        other = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            department_id=dept.id,
            task_id=task.id,
            action_type="tool_send",
            status="pending",
            title="A wants to call mail_send",
            payload={"tool": "mail_send", "arguments": {}, "run_id": str(turn1.id)},
        )
        db.add_all([approval, other])
        await db.flush()
        return turn2.id, approval.id, other.id, agent.id, task.id


async def _drive_worker(run_id: uuid.UUID, tenant: uuid.UUID) -> None:
    class _MustNotRun:
        async def execute(self, db: Any, **kw: Any) -> Any:
            raise AssertionError("a stopped parked run must not execute")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_MustNotRun(),
    )


@pytest.mark.parametrize(
    ("door", "expected_status"),
    [("/stop", 202), ("/cancel", 200)],
)
async def test_stop_ends_a_run_parked_on_an_approval(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    door: str,
    expected_status: int,
) -> None:
    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id, other_id, agent_id, task_id = await _parked_second_turn(
        app_session, tenant
    )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/api/v1/runs/{run_id}{door}", headers=_h(tenant, "alice"))
            assert r.status_code == expected_status, r.text

    async with app_session(tenant) as db:
        approval = await db.get(m.ApprovalRequest, approval_id)
        other = await db.get(m.ApprovalRequest, other_id)
        run = await db.get(m.AgentRun, run_id)
        assert approval is not None and approval.status == "expired"
        assert other is not None and other.status == "pending"
        assert run is not None and run.state == RunState.QUEUED.value
    assert queue.enqueued == [run_id]

    await _drive_worker(run_id, tenant)

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        agent = await db.get(m.Agent, agent_id)
        task = await db.get(m.Task, task_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        assert agent is not None and agent.status == "idle"
        assert task is not None and task.state != "waiting_for_approval"
        cancels = (
            (await db.execute(select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(cancels) == 1


async def test_a_stop_racing_an_approve_leaves_the_resumed_run_alone(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of F6: the Approve locks the card and resumes the run; a Stop
    that read the run as parked just before must not, once the card's lock is
    released, put the now-running run back into the queue."""
    import asyncio

    from sqlalchemy import update

    from oc8.runtime.cancellation import _wake_parked_run

    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id, _other, _agent, _task = await _parked_second_turn(app_session, tenant)

    async with app_session(tenant) as approver:
        # The Approve: card locked first, then the run resumed and picked up.
        card = (
            await approver.execute(
                select(m.ApprovalRequest)
                .where(m.ApprovalRequest.id == approval_id)
                .with_for_update()
            )
        ).scalar_one()
        card.status = "approved"
        await approver.execute(
            update(m.AgentRun).where(m.AgentRun.id == run_id).values(state=RunState.RUNNING.value)
        )
        await approver.flush()

        async def stop() -> None:
            async with app_session(tenant) as db:
                run = await db.get(m.AgentRun, run_id)
                assert run is not None
                await _wake_parked_run(db, run)

        stopping = asyncio.create_task(stop())
        await asyncio.sleep(0.5)  # the Stop reads the run as parked, then waits on the card
    await asyncio.wait_for(stopping, timeout=10)

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        card_after = await db.get(m.ApprovalRequest, approval_id)
        assert run is not None and run.state == RunState.RUNNING.value
        assert card_after is not None and card_after.status == "approved"
    assert queue.enqueued == []


async def test_a_stop_finding_the_run_already_queued_does_nothing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.runtime.cancellation import _wake_parked_run

    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id, _other, _agent, _task = await _parked_second_turn(app_session, tenant)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.state = RunState.QUEUED.value
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await _wake_parked_run(db, run)
    async with app_session(tenant) as db:
        card = await db.get(m.ApprovalRequest, approval_id)
        assert card is not None and card.status == "pending"
    assert queue.enqueued == []


async def test_a_stop_just_before_the_run_parks_still_ends_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of F6: a Stop that lands while the run is still running finds
    nothing parked to wake; the run then parks on its card and, without this,
    waits for ever. The park itself now sees the recorded cancel."""
    import datetime as dt

    from sqlalchemy import text as sql_text

    from oc8.agent.engine import RunResult

    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id, other_id, _agent, task_id = await _parked_second_turn(app_session, tenant)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.state = RunState.QUEUED.value

    class _StoppedWhileRunning:
        async def execute(self, db: Any, **kw: Any) -> RunResult:
            # Release the executor's row lock, as the real runtimes do.
            await db.commit()
            await db.execute(
                sql_text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant)}
            )
            async with app_session(tenant) as stop:
                stop.add(
                    m.RunCancellation(
                        tenant_id=tenant,
                        run_id=run_id,
                        requested_at=dt.datetime.now(tz=dt.UTC),
                        cancellation_kind="operator",
                    )
                )
            return RunResult(
                task_id=task_id,
                agent_id=kw["agent"].id,
                status="waiting_for_approval",
                output="",
                tool_calls=[],
                steps=1,
            )

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_StoppedWhileRunning(),
    )

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        approval = await db.get(m.ApprovalRequest, approval_id)
        other = await db.get(m.ApprovalRequest, other_id)
        assert run is not None and run.state == RunState.QUEUED.value
        assert approval is not None and approval.status == "expired"
        assert other is not None and other.status == "pending"
    assert queue.enqueued == [run_id]

    await _drive_worker(run_id, tenant)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value


async def test_a_stop_on_a_run_another_writer_holds_still_records_the_cancel(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review of F6: the wake must never cost the Stop itself. A run
    row another writer holds past the wait leaves the run as it is, but the
    cancel commits -- the run honours it at its next step or park."""
    import asyncio

    from oc8.runtime.cancellation import record_operator_cancel

    monkeypatch.setattr("oc8.runtime.cancellation._WAKE_LOCK_TIMEOUT", "200ms")
    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id, _other, _agent, _task = await _parked_second_turn(app_session, tenant)

    async with app_session(tenant) as holder:
        await holder.execute(select(m.AgentRun).where(m.AgentRun.id == run_id).with_for_update())

        async def stop() -> None:
            async with app_session(tenant) as db:
                run = await db.get(m.AgentRun, run_id)
                assert run is not None
                await record_operator_cancel(db, run=run, principal=None, actor_id=None)

        await asyncio.wait_for(stop(), timeout=10)

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        card = await db.get(m.ApprovalRequest, approval_id)
        cancels = (
            (await db.execute(select(m.RunCancellation).where(m.RunCancellation.run_id == run_id)))
            .scalars()
            .all()
        )
        assert run is not None and run.state == RunState.WAITING_FOR_APPROVAL.value
        assert card is not None and card.status == "pending"
        assert len(cancels) == 1
    assert queue.enqueued == []


async def test_a_parked_run_is_woken_exactly_once(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.runtime.cancellation import _wake_parked_run

    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id, _other, _agent, _task = await _parked_second_turn(app_session, tenant)
    for _ in range(2):
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            await _wake_parked_run(db, run)
    async with app_session(tenant) as db:
        card = await db.get(m.ApprovalRequest, approval_id)
        history = (
            (
                await db.execute(
                    select(m.RunStateTransition).where(
                        m.RunStateTransition.run_id == run_id,
                        m.RunStateTransition.to_state == RunState.QUEUED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert card is not None and card.status == "expired"
        assert len(history) == 1
    assert queue.enqueued == [run_id]
