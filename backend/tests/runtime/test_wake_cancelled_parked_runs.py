"""The reconciler's second chance for a stop on a parked run (E2E F6).

`record_operator_cancel` wakes a run parked on an approval at once, but a wake
that finds the row locked gives up rather than wait: the cancel row commits and
the run stays parked. The run worker's timer then finds every parked run with a
recorded cancel and wakes it through the same compare-and-set; a row still
locked is skipped and tried again on the next tick.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.runtime.reconcile import wake_cancelled_parked_runs
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory
from tests.runtime.test_stop_parked_run import (
    _drive_worker,
    _parked_second_turn,
    _RecordingQueue,
)

pytestmark = pytest.mark.asyncio


async def _parked_with_a_cancel(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, cancelled: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    """A parked run whose cancel was recorded but whose wake never happened."""
    async with app_session(tenant) as db:
        # The sweep discovers tenants the way the cron scheduler does.
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()
    run_id, approval_id, _other, _agent, _task = await _parked_second_turn(app_session, tenant)
    if cancelled:
        async with app_session(tenant) as db:
            db.add(
                m.RunCancellation(
                    tenant_id=tenant,
                    run_id=run_id,
                    requested_at=dt.datetime.now(tz=dt.UTC),
                    cancellation_kind="operator_interrupted",
                )
            )
    return run_id, approval_id


async def test_one_sweep_ends_a_parked_run_whose_wake_never_came(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id = await _parked_with_a_cancel(app_session, tenant)

    woken = await wake_cancelled_parked_runs()

    assert run_id in woken
    assert queue.enqueued == [run_id]
    await _drive_worker(run_id, tenant)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        card = await db.get(m.ApprovalRequest, approval_id)
        assert run is not None and run.state == RunState.INTERRUPTED.value
        assert card is not None and card.status == "expired"


async def test_a_parked_run_nobody_stopped_is_left_parked(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id = await _parked_with_a_cancel(app_session, tenant, cancelled=False)

    woken = await wake_cancelled_parked_runs()

    assert run_id not in woken
    assert run_id not in queue.enqueued
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        card = await db.get(m.ApprovalRequest, approval_id)
        assert run is not None and run.state == RunState.WAITING_FOR_APPROVAL.value
        assert card is not None and card.status == "pending"


async def test_a_locked_parked_run_is_skipped_and_woken_on_the_next_sweep(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.runtime.cancellation._WAKE_LOCK_TIMEOUT", "200ms")
    queue = _RecordingQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    tenant = uuid.uuid4()
    run_id, approval_id = await _parked_with_a_cancel(app_session, tenant)

    async with app_session(tenant) as holder:
        await holder.execute(
            select(m.AgentRun).where(m.AgentRun.id == run_id).with_for_update()
        )
        assert run_id not in await wake_cancelled_parked_runs()

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        card = await db.get(m.ApprovalRequest, approval_id)
        assert run is not None and run.state == RunState.WAITING_FOR_APPROVAL.value
        assert card is not None and card.status == "pending"
    assert queue.enqueued == []

    assert run_id in await wake_cancelled_parked_runs()
    assert queue.enqueued == [run_id]
