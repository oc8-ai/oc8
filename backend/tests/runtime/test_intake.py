"""Tests for the enqueue_run() intake service (§14.1/§8.3).

Owns dedup (idempotency_key), coalescing (coalesce_key), and the
commit-before-enqueue ordering. Queue isolation mirrors
tests/triggers/test_scheduler.py: a per-test RunQueue stream key injected by
monkeypatching the module-level get_run_queue lookup.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.runtime.intake import enqueue_run
from oc8.runtime.queue import RunMessage, RunQueue
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from oc8.tenants.activity import TenantInactive, set_tenant_activity_gate
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _isolated_queue(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> RunQueue:
    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    return queue


async def _drain(queue: RunQueue) -> list[RunMessage]:
    messages: list[RunMessage] = []
    while (msg := await queue.dequeue(timeout=1.0)) is not None:
        messages.append(msg)
    return messages


async def test_plain_enqueue_creates_and_publishes(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run, published = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={"task": "hello"},
                source="manual",
            )
        run_id = run.id
        assert published is True
        assert run.state == RunState.QUEUED.value

        messages = await _drain(queue)
        assert len(messages) == 1
        assert messages[0]["run_id"] == str(run_id)
        assert messages[0]["tenant_id"] == str(tenant)
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        row = await db.get(m.AgentRun, run_id)
        assert row is not None
        assert row.source == "manual"


async def test_same_idempotency_key_returns_existing(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run1, pub1 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                idempotency_key="K1",
            )
        async with app_session(tenant) as db:
            run2, pub2 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                idempotency_key="K1",
            )
        assert pub1 is True
        assert pub2 is False
        assert run1.id == run2.id

        messages = await _drain(queue)
        assert len(messages) == 1
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.idempotency_key == "K1")))
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_idempotency_dedup_survives_terminal_state(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run1, _ = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                idempotency_key="K2",
            )
        run1_id = run1.id

        # Drive the run to a terminal state (queued -> running -> done).
        async with app_session(tenant) as db:
            repo = RunRepository(db)
            row = await repo.get(run1_id)
            assert row is not None
            await repo.transition(row, RunState.RUNNING)
            await repo.transition(row, RunState.DONE)

        async with app_session(tenant) as db:
            run2, pub2 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                idempotency_key="K2",
            )
        assert pub2 is False
        assert run2.id == run1_id
        assert run2.state == RunState.DONE.value
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.idempotency_key == "K2")))
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_idempotency_conflict_path_keeps_tenant_binding(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The savepoint test. A row with key K is pre-inserted AND committed, so
    enqueue_run's INSERT loses the unique-index race and raises IntegrityError.
    The follow-up SELECT must still return the existing row -- which only holds
    if the IntegrityError unwinds a SAVEPOINT (leaving the outer transaction and
    its transaction-local app.tenant_id binding alive) rather than a bare
    rollback() (which would end the transaction, unbind the tenant, and make the
    RLS-scoped SELECT return zero rows -> NoResultFound)."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            repo = RunRepository(db)
            pre = await repo.create(
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                idempotency_key="K3",
            )
        pre_id = pre.id

        async with app_session(tenant) as db:
            run, published = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                idempotency_key="K3",
            )
        assert published is False
        assert run.id == pre_id

        messages = await _drain(queue)
        assert len(messages) == 0
    finally:
        await queue.close()


async def test_coalesce_increments_instead_of_stacking(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run1, pub1 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                coalesce_key="C1",
            )
        async with app_session(tenant) as db:
            run2, pub2 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                coalesce_key="C1",
            )
        assert pub1 is True
        assert pub2 is False
        assert run1.id == run2.id
        assert run2.coalesced_count == 1

        messages = await _drain(queue)
        assert len(messages) == 1
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.coalesce_key == "C1")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].coalesced_count == 1


async def test_an_event_fire_is_never_folded_onto_a_working_run(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default, and why it is the default. An event fires because something
    specific happened; a run already past the point where it would have noticed
    will never go back for it, so folding would drop the event silently. Only a
    SCHEDULE repeats the same instruction, and only it asks for fold_running."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run1, _ = await enqueue_run(
                db, tenant_id=tenant, agent_id=agent, context={},
                source="event", coalesce_key="C4",
            )
        run1_id = run1.id
        async with app_session(tenant) as db:
            repo = RunRepository(db)
            row = await repo.get(run1_id)
            assert row is not None
            await repo.transition(row, RunState.RUNNING)

        async with app_session(tenant) as db:
            run2, pub2 = await enqueue_run(
                db, tenant_id=tenant, agent_id=agent, context={},
                source="event", coalesce_key="C4",
            )
        assert pub2 is True
        assert run2.id != run1_id
    finally:
        await queue.close()


async def test_idempotency_key_bypasses_coalescing(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (I1): an event fire carries BOTH coalesce_key and
    idempotency_key. Coalescing must NOT short-circuit ahead of the key and
    drop it -- otherwise a redelivery of the same delivery_id creates a second
    run because the key was never persisted. With a queued run R1 present under
    the same coalesce_key, a fire that also supplies an idempotency_key must
    skip coalescing, create a NEW run, persist the key, and let the key dedupe
    the redelivery."""
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        # R1: an earlier delivery still queued under this trigger's coalesce_key.
        async with app_session(tenant) as db:
            run1, pub1 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="event",
                coalesce_key="trigger:T",
            )
        run1_id = run1.id
        assert pub1 is True

        # D2: a distinct delivery for the same trigger, carrying an
        # idempotency_key. Must NOT coalesce onto R1 -- it must create a new run
        # that persists the key.
        async with app_session(tenant) as db:
            run2, pub2 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="event",
                coalesce_key="trigger:T",
                idempotency_key="k2",
            )
        run2_id = run2.id
        assert pub2 is True
        assert run2_id != run1_id
        assert run2.idempotency_key == "k2"

        # R1 was untouched -- coalescing never ran.
        async with app_session(tenant) as db:
            r1 = await db.get(m.AgentRun, run1_id)
            assert r1 is not None
            assert r1.coalesced_count == 0

        # Redelivery of D2 (GitHub retry): the persisted key now dedupes it.
        async with app_session(tenant) as db:
            run3, pub3 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="event",
                coalesce_key="trigger:T",
                idempotency_key="k2",
            )
        assert pub3 is False
        assert run3.id == run2_id

        messages = await _drain(queue)
        # R1 published + D2 published; the redelivery did not.
        assert len(messages) == 2
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.idempotency_key == "k2")))
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_coalesce_folds_onto_a_run_that_is_already_working(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run1, _ = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                coalesce_key="C2",
            )
        run1_id = run1.id

        async with app_session(tenant) as db:
            repo = RunRepository(db)
            row = await repo.get(run1_id)
            assert row is not None
            await repo.transition(row, RunState.RUNNING)

        async with app_session(tenant) as db:
            run2, pub2 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="manual",
                coalesce_key="C2",
                fold_running=True,
            )
        # A repeat fire while the first one is still WORKING is the same
        # instruction twice, not a second piece of work. Folding it only while
        # the earlier run had not started yet meant a second container for one
        # agent, both reaching for the same ticket -- observed live, two
        # containers named for the same agent.
        assert pub2 is False
        assert run2.id == run1_id
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.coalesce_key == "C2")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].coalesced_count == 1


async def test_coalesce_scoped_to_agent(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            _, pub1 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent_a,
                context={},
                source="manual",
                coalesce_key="C3",
            )
        async with app_session(tenant) as db:
            _, pub2 = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent_b,
                context={},
                source="manual",
                coalesce_key="C3",
            )
        assert pub1 is True
        assert pub2 is True
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.coalesce_key == "C3")))
            .scalars()
            .all()
        )
        assert len(rows) == 2


async def test_different_sources_persist(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()
    try:
        async with app_session(tenant) as db:
            run, published = await enqueue_run(
                db,
                tenant_id=tenant,
                agent_id=agent,
                context={},
                source="cron",
            )
        run_id = run.id
        assert published is True
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        row = await db.get(m.AgentRun, run_id)
        assert row is not None
        assert row.source == "cron"


async def test_enqueue_run_refuses_an_inactive_organisation(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _isolated_queue(redis_url, monkeypatch)
    tenant = uuid.uuid4()
    agent = uuid.uuid4()

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)
    try:
        with pytest.raises(TenantInactive) as raised:
            async with app_session(tenant) as db:
                await enqueue_run(
                    db,
                    tenant_id=tenant,
                    agent_id=agent,
                    context={"task": "hello"},
                    source="manual",
                )
        assert raised.value.tenant_id == tenant
        assert await _drain(queue) == []
    finally:
        await queue.close()

    async with app_session(tenant) as db:
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent)))
            .scalars()
            .all()
        )
        assert runs == []
