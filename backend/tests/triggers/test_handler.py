from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.events.types import InboundEvent
from oc8.runtime.queue import RunQueue
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from oc8.tenants.activity import set_tenant_activity_gate
from oc8.triggers.handler import handle_inbound_event
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


def _unique_event_type(label: str) -> str:
    """handle_inbound_event searches ACROSS ALL TENANTS by (source, type), and
    a fired trigger row is never deleted -- reusing a literal event_type like
    "github.issues.opened" across tests would let one test's still-enabled
    trigger get re-matched and re-fired by a later, unrelated test's dispatch
    of the "same" event. Each test gets its own type so no other test's
    trigger row can ever match it."""
    return f"github.issues.opened.{label}.{uuid.uuid4().hex[:12]}"


async def test_handle_inbound_event_fires_matching_event_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("fires")

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="event",
            task_text="respond to issue",
            event_source="github",
            event_type=event_type,
        )
        trigger_id = trigger.id

    try:
        await handle_inbound_event(InboundEvent(source="github", type=event_type, payload={}))
        message = await test_queue.dequeue(timeout=2.0)
        assert message is not None
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is not None


async def test_handle_inbound_event_fires_all_matching_triggers_for_one_tenant(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: fire_trigger() commits, which ends the transaction
    that carries the tenant's RLS binding. If this handler shared one
    tenant_session across multiple fires, only the first matching trigger
    for a tenant would fire -- the second's INSERT would run unbound and be
    rejected by agent_run's RLS WITH CHECK. Pins two agents both subscribed
    to the same event under one tenant both firing from a single dispatch."""
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("multi")

    trigger_ids = []
    async with app_session(tenant) as db:
        for label in ("a", "b"):
            trigger = await create_trigger(
                db,
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                kind="event",
                task_text=label,
                event_source="github",
                event_type=event_type,
            )
            trigger_ids.append(trigger.id)

    try:
        await handle_inbound_event(InboundEvent(source="github", type=event_type, payload={}))
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


async def test_handle_inbound_event_ignores_non_matching_type(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("ignores")

    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="event",
            task_text="respond to issue",
            event_source="github",
            event_type=event_type,
        )
        trigger_id = trigger.id

    try:
        await handle_inbound_event(
            InboundEvent(source="github", type=f"{event_type}.other", payload={})
        )
        message = await test_queue.dequeue(timeout=1.0)
        assert message is None
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is None


async def test_fired_run_records_source(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("source")
    agent_id = uuid.uuid4()

    async with app_session(tenant) as db:
        await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="event",
            task_text="respond to issue",
            event_source="github",
            event_type=event_type,
        )

    try:
        await handle_inbound_event(InboundEvent(source="github", type=event_type, payload={}))
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].source == "event"


async def test_handle_inbound_event_never_fires_cron_kind_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("cron-guard")

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
        # A cron-kind row has event_source/event_type == None, so it can never
        # match an InboundEvent's (source, type) equality filter -- this pins
        # that behavior down explicitly rather than relying on it implicitly.
        # event_type is unique to this test so no other test's event-kind
        # trigger row can spuriously match instead.
        await handle_inbound_event(InboundEvent(source="github", type=event_type, payload={}))
        message = await test_queue.dequeue(timeout=1.0)
        assert message is None
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.last_run_at is None


async def test_same_delivery_id_fires_once(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redelivery dedup rests on idempotency_key, not coalesce_key -- while a
    run is still QUEUED, coalesce_key alone already collapses repeat fires
    (see tests/runtime/test_intake.py), so this test drives the first run out
    of QUEUED before redelivering to pin the idempotency_key mechanism Task 6
    adds, not the pre-existing coalescing."""
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("dedup-once")
    agent_id = uuid.uuid4()
    event = InboundEvent(
        source="github", type=event_type, payload={}, delivery_id=str(uuid.uuid4())
    )

    async with app_session(tenant) as db:
        await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="event",
            task_text="respond to issue",
            event_source="github",
            event_type=event_type,
        )

    try:
        await handle_inbound_event(event)

        async with app_session(tenant) as db:
            rows = (
                (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            await RunRepository(db).transition(rows[0], RunState.RUNNING)

        await handle_inbound_event(event)
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_delivery_id_dedup_is_per_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One delivery matching two triggers must fire both -- once each -- and a
    redelivery of that same delivery must add no further runs to either."""
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("dedup-per-trigger")
    agent_ids = [uuid.uuid4(), uuid.uuid4()]
    event = InboundEvent(
        source="github", type=event_type, payload={}, delivery_id=str(uuid.uuid4())
    )

    async with app_session(tenant) as db:
        for label, agent_id in zip(("a", "b"), agent_ids, strict=True):
            await create_trigger(
                db,
                tenant_id=tenant,
                agent_id=agent_id,
                kind="event",
                task_text=label,
                event_source="github",
                event_type=event_type,
            )

    try:
        await handle_inbound_event(event)

        async with app_session(tenant) as db:
            rows = (
                (
                    await db.execute(
                        select(m.AgentRun).where(m.AgentRun.agent_id.in_(agent_ids))
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2
            repo = RunRepository(db)
            for row in rows:
                await repo.transition(row, RunState.RUNNING)

        await handle_inbound_event(event)
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id.in_(agent_ids))))
            .scalars()
            .all()
        )
        assert len(rows) == 2


async def test_no_delivery_id_fires_every_time(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documents today's behavior explicitly: without a delivery_id there is no
    idempotency_key, so once a fire's run leaves QUEUED (so coalesce_key no
    longer applies either) a second dispatch of the same event creates a
    second, independent run."""
    tenant = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("no-delivery-id")
    agent_id = uuid.uuid4()
    event = InboundEvent(source="github", type=event_type, payload={}, delivery_id=None)

    async with app_session(tenant) as db:
        await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=agent_id,
            kind="event",
            task_text="respond to issue",
            event_source="github",
            event_type=event_type,
        )

    try:
        await handle_inbound_event(event)

        async with app_session(tenant) as db:
            rows = (
                (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            await RunRepository(db).transition(rows[0], RunState.RUNNING)

        await handle_inbound_event(event)
    finally:
        await test_queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2


async def test_event_fan_out_skips_an_inactive_organisation(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _new_tenant_with_org(app_session)
    tenant_b = await _new_tenant_with_org(app_session)
    test_queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: test_queue)
    event_type = _unique_event_type("inactive")

    trigger_ids: dict[uuid.UUID, uuid.UUID] = {}
    for tenant in (tenant_a, tenant_b):
        async with app_session(tenant) as db:
            trigger = await create_trigger(
                db,
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                kind="event",
                task_text="respond to issue",
                event_source="github",
                event_type=event_type,
            )
            trigger_ids[tenant] = trigger.id

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant_a

    set_tenant_activity_gate(gate)
    try:
        await handle_inbound_event(InboundEvent(source="github", type=event_type, payload={}))
    finally:
        await test_queue.close()

    async with app_session(tenant_a) as db:
        refreshed_a = await db.get(m.Trigger, trigger_ids[tenant_a])
        assert refreshed_a is not None
        assert refreshed_a.last_run_at is None
    async with app_session(tenant_b) as db:
        refreshed_b = await db.get(m.Trigger, trigger_ids[tenant_b])
        assert refreshed_b is not None
        assert refreshed_b.last_run_at is not None
