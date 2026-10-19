from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.runtime.queue import RunQueue
from oc8.tenants.activity import TenantActivityGate, set_tenant_activity_gate
from oc8.triggers.scheduler import list_active_tenant_ids, run_scheduler_tick
from oc8.triggers.service import create_trigger
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _new_tenant_with_org(app_session: AppSessionFactory) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"test-{tenant.hex[:12]}",
                name="Test Org",
                tier="standard",
                region="eu",
            )
        )
        await db.flush()
    return tenant


async def test_list_active_tenant_ids_includes_seeded_organization(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    assert tenant in await list_active_tenant_ids()


async def test_tick_fires_due_cron_trigger_and_enqueues(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    agent_id = uuid.uuid4()

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="daily",
            cron_expression="0 9 * * *",
        )
        trigger_id = trigger.id
        # Force it due right now (compute_next_run always returns a future time).
        trigger.next_run_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        await db.flush()

    try:
        fired = await run_scheduler_tick()
        assert fired >= 1

        message = await test_queue.dequeue(timeout=2.0)
        assert message is not None
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is not None
        assert refreshed.next_run_at is not None
        assert refreshed.next_run_at > datetime.now(tz=UTC)


async def test_tick_fires_all_due_triggers_for_one_tenant_not_just_the_first(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: fire_trigger() commits, which ends the transaction
    that carries the tenant's RLS binding (set_config(..., is_local=true)
    is transaction-local). If the tick shared one tenant_session across
    multiple fires, only the first trigger in a tenant's due list would
    fire -- the second's INSERT would run unbound and be rejected by
    agent_run's RLS WITH CHECK, silently dropping every trigger after the
    first. This pins two due triggers under the same tenant both firing."""
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)

    trigger_ids = []
    async with app_session(tenant) as db:
        for label in ("a", "b"):
            trigger = await create_trigger(
                db,
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                kind="cron",
                task_text=label,
                cron_expression="0 9 * * *",
            )
            trigger.next_run_at = datetime.now(tz=UTC) - timedelta(seconds=1)
            trigger_ids.append(trigger.id)
        await db.flush()

    try:
        fired = await run_scheduler_tick()
        assert fired == 2

        messages = []
        while (msg := await test_queue.dequeue(timeout=1.0)) is not None:
            messages.append(msg)
        assert len(messages) == 2
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        for trigger_id in trigger_ids:
            refreshed = await db.get(m.Trigger, trigger_id)
            assert refreshed is not None
            assert refreshed.last_run_at is not None


async def test_tick_skips_not_yet_due_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="daily",
            cron_expression="0 9 * * *",
        )
        trigger_id = trigger.id

    try:
        await run_scheduler_tick()
        message = await test_queue.dequeue(timeout=1.0)
        assert message is None
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is None


async def test_tick_skips_disabled_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="daily",
            cron_expression="0 9 * * *",
        )
        trigger.enabled = False
        trigger.next_run_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        trigger_id = trigger.id
        await db.flush()

    try:
        await run_scheduler_tick()
        message = await test_queue.dequeue(timeout=1.0)
        assert message is None
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is None


async def test_tick_never_fires_event_kind_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="event",
            task_text="respond to issue",
            event_source="github",
            # Namespaced, not the literal "github.issues.opened": this row is
            # never fired/deleted, and handle_inbound_event (Task 3) matches
            # ACROSS ALL TENANTS by (source, type) -- a real webhook-event
            # test dispatching that literal type later in the same run would
            # otherwise match and fire this leftover trigger unexpectedly.
            event_type="github.issues.opened.test-scheduler-ignore",
        )
        trigger_id = trigger.id

    try:
        await run_scheduler_tick()
        message = await test_queue.dequeue(timeout=1.0)
        assert message is None
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is None


async def test_second_tick_coalesces_while_first_fire_is_queued(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first tick fires the trigger and its run sits QUEUED (the worker
    never runs in this test). A second tick, with the trigger forced due
    again, must coalesce onto that still-queued run instead of stacking a
    second one -- while the cron bookkeeping (last_run_at/next_run_at) still
    advances, since the schedule moves on regardless of whether a new run
    was actually created."""
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    agent_id = uuid.uuid4()

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="daily",
            cron_expression="0 9 * * *",
        )
        trigger_id = trigger.id
        trigger.next_run_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        await db.flush()

    try:
        fired1 = await run_scheduler_tick()
        assert fired1 >= 1

        async with app_session(tenant) as db:
            refreshed = await db.get(m.Trigger, trigger_id)
            assert refreshed is not None
            first_last_run_at = refreshed.last_run_at
            assert first_last_run_at is not None
            # Force it due again -- without running the worker, so the run
            # created by the first tick is still sitting QUEUED.
            refreshed.next_run_at = datetime.now(tz=UTC) - timedelta(seconds=1)
            await db.flush()

        fired2 = await run_scheduler_tick()
        assert fired2 >= 1
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].coalesced_count == 1

        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is not None
        assert refreshed.last_run_at > first_last_run_at
        assert refreshed.next_run_at is not None
        assert refreshed.next_run_at > datetime.now(tz=UTC)


async def test_fired_run_records_source(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    agent_id = uuid.uuid4()

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="daily",
            cron_expression="0 9 * * *",
        )
        trigger.next_run_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        await db.flush()

    try:
        fired = await run_scheduler_tick()
        assert fired >= 1
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].source == "cron"


async def test_tick_isolates_tenants_each_fires_only_its_own_due_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _new_tenant_with_org(app_session)
    tenant_b = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)

    async with app_session(tenant_a) as db:
        trigger_a = await create_trigger(
            db,
            tenant_id=tenant_a,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="a",
            cron_expression="0 9 * * *",
        )
        trigger_a.next_run_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        trigger_a_id = trigger_a.id
        await db.flush()

    async with app_session(tenant_b) as db:
        trigger_b = await create_trigger(
            db,
            tenant_id=tenant_b,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="b",
            cron_expression="0 9 * * *",
        )
        # Not due -- next_run_at stays in the future (compute_next_run's default).
        trigger_b_id = trigger_b.id

    try:
        await run_scheduler_tick()
        messages = []
        while (msg := await test_queue.dequeue(timeout=1.0)) is not None:
            messages.append(msg)
        assert len(messages) == 1
        assert messages[0]["tenant_id"] == str(tenant_a)
    finally:
        await test_queue.close()

    async with app_session(tenant_a) as db:
        refreshed_a = await db.get(m.Trigger, trigger_a_id)
        assert refreshed_a is not None
        assert refreshed_a.last_run_at is not None

    async with app_session(tenant_b) as db:
        refreshed_b = await db.get(m.Trigger, trigger_b_id)
        assert refreshed_b is not None
        assert refreshed_b.last_run_at is None


def _gate_saying_no_for(*inactive: uuid.UUID) -> TenantActivityGate:
    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id not in inactive

    return gate


async def _due_cron_trigger(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, cron_expression: str = "0 9 * * *"
) -> tuple[uuid.UUID, uuid.UUID]:
    agent_id = uuid.uuid4()
    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="scheduled",
            cron_expression=cron_expression,
        )
        trigger.next_run_at = datetime.now(tz=UTC) - timedelta(minutes=30)
        trigger_id = trigger.id
        await db.flush()
    return trigger_id, agent_id


async def _runs_for(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID
) -> list[m.AgentRun]:
    async with app_session(tenant) as db:
        return list(
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )


async def test_tick_skips_an_inactive_organisation_and_fires_the_others(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _new_tenant_with_org(app_session)
    tenant_b = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    _, agent_a = await _due_cron_trigger(app_session, tenant_a)
    _, agent_b = await _due_cron_trigger(app_session, tenant_b)
    set_tenant_activity_gate(_gate_saying_no_for(tenant_a))

    try:
        fired = await run_scheduler_tick()
    finally:
        await test_queue.close()

    assert fired == 1
    assert await _runs_for(app_session, tenant_a, agent_a) == []
    assert len(await _runs_for(app_session, tenant_b, agent_b)) == 1


async def test_a_skipped_cron_trigger_is_not_caught_up_later(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    trigger_id, agent_a = await _due_cron_trigger(
        app_session, tenant_a, cron_expression="*/5 * * * *"
    )
    set_tenant_activity_gate(_gate_saying_no_for(tenant_a))

    try:
        await run_scheduler_tick()

        async with app_session(tenant_a) as db:
            refreshed = await db.get(m.Trigger, trigger_id)
            assert refreshed is not None
            assert refreshed.next_run_at is not None
            assert refreshed.next_run_at > datetime.now(tz=UTC)
            assert refreshed.last_run_at is None
        assert await _runs_for(app_session, tenant_a, agent_a) == []

        set_tenant_activity_gate(None)
        await run_scheduler_tick()
    finally:
        await test_queue.close()

    assert await _runs_for(app_session, tenant_a, agent_a) == []


async def test_cleanup_still_visits_an_inactive_organisation(
    app_session: AppSessionFactory,
) -> None:
    tenant_a = await _new_tenant_with_org(app_session)
    set_tenant_activity_gate(_gate_saying_no_for(tenant_a))

    assert tenant_a in await list_active_tenant_ids()


async def test_a_fire_refused_by_the_intake_is_logged_and_the_tick_goes_on(
    app_session: AppSessionFactory,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gate can change between the tick's own check and the intake's: the
    refused fire is logged, rolled back, and the other organisations still fire."""
    tenant_a = await _new_tenant_with_org(app_session)
    tenant_b = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    trigger_a, agent_a = await _due_cron_trigger(app_session, tenant_a)
    _, agent_b = await _due_cron_trigger(app_session, tenant_b)
    set_tenant_activity_gate(_gate_saying_no_for(tenant_a))

    async def may_run(_tenant_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr("oc8.triggers.scheduler.tenant_may_run", may_run)

    try:
        with caplog.at_level(logging.INFO, logger="oc8.triggers.scheduler"):
            fired = await run_scheduler_tick()
    finally:
        await test_queue.close()

    assert fired == 1
    assert await _runs_for(app_session, tenant_a, agent_a) == []
    assert len(await _runs_for(app_session, tenant_b, agent_b)) == 1
    refused = [r for r in caplog.records if str(trigger_a) in r.getMessage()]
    assert refused, "the refused fire is logged"
    # A gate change mid-tick is expected, not an error with a traceback.
    assert all(r.levelno == logging.INFO and r.exc_info is None for r in refused)
    async with app_session(tenant_a) as db:
        refreshed = await db.get(m.Trigger, trigger_a)
        assert refreshed is not None
        assert refreshed.last_run_at is None, "the refused fire leaves the trigger due"


async def test_a_due_follow_up_waits_and_fires_once_after_the_organisation_may_run(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.agent.assistant import get_or_create_assistant
    from oc8.copilot.followups import schedule_followup
    from oc8.copilot.responsibilities import open_responsibility
    from tests.copilot.helpers import copilot_seat, once_in_an_hour

    tenant = await _new_tenant_with_org(app_session)
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        responsibility = await open_responsibility(
            db,
            tenant_id=tenant,
            member_id=seat.member_id,
            chat_session_id=seat.session_id,
            title="Offsite",
            goal="Keep the offsite on track",
            origin_channel=None,
            run_id=None,
            actor_agent_id=assistant.id,
            member_subject=seat.subject,
        )
        trigger = await schedule_followup(
            db,
            tenant_id=tenant,
            member_id=seat.member_id,
            responsibility_id=responsibility.id,
            assistant_id=assistant.id,
            spec=once_in_an_hour(),
            prompt="Check the venue replies",
            member_subject=seat.subject,
        )
        assert trigger.kind == "once" and trigger.chat_session_id == seat.session_id
        due_at = datetime.now(tz=UTC) - timedelta(minutes=5)
        trigger.next_run_at = due_at
        trigger_id = trigger.id
        await db.flush()

    async def chat_runs() -> list[m.AgentRun]:
        async with app_session(tenant) as db:
            return list(
                (
                    await db.execute(
                        select(m.AgentRun).where(
                            m.AgentRun.tenant_id == tenant, m.AgentRun.source == "chat"
                        )
                    )
                )
                .scalars()
                .all()
            )

    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    set_tenant_activity_gate(_gate_saying_no_for(tenant))
    try:
        await run_scheduler_tick()

        async with app_session(tenant) as db:
            waiting = await db.get(m.Trigger, trigger_id)
            assert waiting is not None
            assert waiting.enabled is True
            assert waiting.next_run_at == due_at
            assert waiting.last_run_at is None
        assert await chat_runs() == []

        set_tenant_activity_gate(None)
        await run_scheduler_tick()
        await run_scheduler_tick()
    finally:
        await test_queue.close()

    assert len(await chat_runs()) == 1
    async with app_session(tenant) as db:
        spent = await db.get(m.Trigger, trigger_id)
        assert spent is not None and spent.enabled is False
