"""The only place an AgentRun is created and published. Owns dedup
(idempotency_key), coalescing (coalesce_key), and the commit-before-enqueue
ordering."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.models.run import AgentRun
from oc8.runtime.queue import get_run_queue
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState


async def enqueue_run(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    context: dict[str, Any],
    source: str,
    idempotency_key: str | None = None,
    coalesce_key: str | None = None,
    fold_running: bool = False,
    task_id: uuid.UUID | None = None,
) -> tuple[AgentRun, bool]:
    """Create-and-publish a run, or reuse an existing one. The single intake
    point every run source funnels through.

    Returns (run, published). ``published=False`` means an existing run was
    reused -- deduped by ``idempotency_key`` or coalesced onto a still-queued
    run by ``coalesce_key`` -- and nothing new was put on the stream.

    ``task_id`` hands the run a task that already exists instead of letting the
    engine open one when it starts (see ``open_run_task``). Callers that create
    the task themselves -- an accepted handoff becoming work on a lead's desk --
    must pass it here rather than setting it afterwards: a worker can claim the
    run in between and open a second task for the same piece of work.

    COMMITS ``db``. ``set_config('app.tenant_id', ..., is_local => true)`` is
    transaction-local, so the tenant binding ends at that commit: callers must
    not issue further queries on ``db`` afterwards. Already-loaded attributes on
    the returned run stay readable because the sessionmaker uses
    ``expire_on_commit=False``.

    Coalescing is a best-effort optimisation, not a correctness guarantee: two
    concurrent fires that both observe no queued row can each create a run.
    ``idempotency_key`` -- backed by a unique index -- is the correctness
    mechanism; coalescing only collapses the common case where a repeated fire
    lands while the prior run is still waiting to start.

    Coalescing is bypassed entirely whenever an ``idempotency_key`` is supplied,
    so the two mechanisms never compete: were coalescing allowed to run first it
    could match a still-queued sibling and return WITHOUT ever persisting the
    key, so a later redelivery of the same delivery would find no unique-index
    conflict and create a duplicate run. With a key present, the create-with-key
    path is the sole dedup and the key is always stored.
    """
    # 1. Coalesce (best-effort): fold a repeat fire onto a still-queued run so
    #    it doesn't stack a second identical job. RLS scopes this to the tenant.
    #    Gate on ``idempotency_key is None``: coalescing only matches
    #    ``state='queued'`` and is best-effort, whereas idempotency_key is the
    #    exact-once mechanism. If a key is supplied, coalescing must NOT
    #    short-circuit ahead of the create-with-key path and drop the key --
    #    that would leave the key unpersisted and let a redelivery create a
    #    duplicate run. So skip coalescing entirely and let the savepoint +
    #    partial unique index be the sole dedup, guaranteeing the key is stored.
    if coalesce_key is not None and idempotency_key is None:
        existing = (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.agent_id == agent_id,
                    AgentRun.coalesce_key == coalesce_key,
                    # `fold_running` widens this to a run already working, and
                    # only a CRON fire asks for it: a poll that fires while the
                    # previous poll is still sweeping is the same instruction
                    # twice, and starting a second container for it gave one
                    # agent two containers reaching for the same ticket.
                    #
                    # An EVENT fire must NOT be folded that way. It fires because
                    # something specific happened, and a run already past the
                    # point where it would have noticed will never go back for
                    # it -- the event would be silently dropped.
                    AgentRun.state.in_(
                        [RunState.QUEUED.value, RunState.RUNNING.value]
                        if fold_running
                        else [RunState.QUEUED.value]
                    ),
                )
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.coalesced_count += 1
            await db.commit()
            return existing, False

    # 2. Create inside a SAVEPOINT. If a concurrent intake already committed a
    #    run with this idempotency_key, the unique index raises IntegrityError;
    #    only the savepoint unwinds, leaving the outer transaction -- and its
    #    transaction-local tenant binding -- alive, so the follow-up SELECT is
    #    still RLS-scoped and can see the row that won the race.
    # `repo.create` pins the run to the agent's current version -- see its
    # docstring; every run-creation path gets that from the same funnel.
    repo = RunRepository(db)
    try:
        async with db.begin_nested():
            run = await repo.create(
                tenant_id=tenant_id,
                agent_id=agent_id,
                context=context,
                source=source,
                idempotency_key=idempotency_key,
                coalesce_key=coalesce_key,
                task_id=task_id,
            )
    except IntegrityError:
        existing = (
            await db.execute(
                select(AgentRun).where(AgentRun.idempotency_key == idempotency_key)
            )
        ).scalar_one()
        await db.commit()
        return existing, False

    # 3. Commit, THEN publish -- never the other way round. The worker's
    #    XREADGROUP can return and load the run in its own session the instant
    #    the stream entry exists, so the row must be durably committed first.
    await db.commit()
    await get_run_queue().enqueue(run_id=run.id, tenant_id=tenant_id)
    return run, True


async def publish_run(*, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Re-publish an already-persisted run onto the stream without creating a
    new one (e.g. the clarification path resuming a suspended run)."""
    await get_run_queue().enqueue(run_id=run_id, tenant_id=tenant_id)
