"""Cron scheduler (§8.4): tick periodically, find due cron triggers across
all tenants (RLS-safe via one bound session per tenant, discovered through
Organization's unbound-read policy -- see migration 0013), fire them via
the durable-run queue."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.runtime.intake import enqueue_run
from oc8.triggers.service import compute_next_run

logger = logging.getLogger(__name__)


async def list_active_tenant_ids() -> list[uuid.UUID]:
    """Every known tenant. Organization's RLS policy (migration 0013) grants
    an unbound session (tenant_session(None)) read access to every row --
    a bound session still only ever sees its own -- specifically so this
    tenant-discovery read works without a per-tenant loop to bootstrap it."""
    async with tenant_session(None) as db:
        result = await db.execute(select(m.Organization.id))
        return list(result.scalars().all())


async def fire_trigger(
    db: AsyncSession,
    trigger: m.Trigger,
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Enqueue an AgentRun for `trigger` -- the same durable intake path
    POST /agents/{id}/run uses. Records last_run_at for any kind, and
    (cron-kind only) recomputes next_run_at.

    The bookkeeping mutations happen *before* enqueue_run so they ride along
    in the same transaction it commits -- and they advance even when
    enqueue_run coalesces this fire onto a still-queued run rather than
    creating a new one: the schedule must move on regardless of whether a
    new run was actually created, or a trigger stuck behind a slow run would
    fire every tick instead of on its own cadence.

    `payload` (kind='webhook' only) is the caller's raw JSON body. There is
    no per-source parsing to extract a "the thing that happened" summary
    from it -- unlike kind='event', a webhook can carry literally anything --
    so it is folded straight into the task text the model reads, the only
    context field build_run_preamble actually surfaces (agent/preamble.py).
    """
    now = datetime.now(tz=UTC)
    trigger.last_run_at = now
    if trigger.kind == "cron" and trigger.cron_expression is not None:
        trigger.next_run_at = compute_next_run(trigger.cron_expression, after=now)
    task = trigger.task_text
    if payload is not None:
        task = f"{task}\n\nWebhook payload:\n{json.dumps(payload, indent=2, default=str)}"
    await enqueue_run(
        db,
        tenant_id=tenant_id,
        agent_id=trigger.agent_id,
        context={"task": task},
        source=trigger.kind,
        coalesce_key=f"trigger:{trigger.id}",
        idempotency_key=idempotency_key,
        # Only a schedule repeats the SAME instruction. An event/webhook fire
        # carries a thing that happened, and folding it onto a run already
        # past that point would drop it.
        fold_running=trigger.kind == "cron",
    )


async def run_scheduler_tick() -> int:
    """One tick: find and fire every due, enabled cron trigger, across every
    tenant. Returns the number of triggers fired.

    Each fire opens its own tenant_session -- fire_trigger() commits inside
    it, and set_config(..., is_local=true) is transaction-local, so sharing
    one session across multiple fires would silently drop the tenant
    binding after the first commit (the next INSERT would fail RLS's
    WITH CHECK with no tenant bound). One session per fire matches how
    POST /agents/{id}/run already works: one request, one session, one
    commit."""
    fired = 0
    for tenant_id in await list_active_tenant_ids():
        try:
            async with tenant_session(tenant_id) as db:
                now = datetime.now(tz=UTC)
                result = await db.execute(
                    select(m.Trigger.id).where(
                        m.Trigger.kind.in_(("cron", "once")),
                        m.Trigger.enabled.is_(True),
                        m.Trigger.next_run_at <= now,
                    )
                )
                due_ids = list(result.scalars().all())
        except Exception:
            logger.exception("scheduler tick failed to list due triggers for tenant %s", tenant_id)
            continue
        for trigger_id in due_ids:
            try:
                async with tenant_session(tenant_id) as db:
                    # CLAIM the trigger before firing it. Listing due triggers
                    # and then firing them is two statements, and a second
                    # scheduler running between them fires the same trigger
                    # again. Coalescing would usually fold that, but "usually"
                    # is not a property to build a schedule on -- and it folds
                    # nothing at all for an event trigger, which must not be
                    # folded. Locking the row and re-checking that it is still
                    # due makes exactly one scheduler the winner.
                    trigger = (
                        await db.execute(
                            select(m.Trigger)
                            .where(
                                m.Trigger.id == trigger_id,
                                m.Trigger.enabled.is_(True),
                                m.Trigger.next_run_at <= datetime.now(tz=UTC),
                            )
                            .with_for_update(skip_locked=True)
                        )
                    ).scalar_one_or_none()
                    if trigger is None:
                        continue  # another scheduler took it, or it is no longer due
                    if trigger.chat_session_id is not None:
                        from oc8.copilot.followups import fire_followup

                        await fire_followup(db, trigger, tenant_id=tenant_id)
                    elif trigger.kind == "once":
                        # A one-shot only ever exists as a follow-up in a chat
                        # session; without one there is nothing to fire into.
                        trigger.enabled = False
                        continue
                    else:
                        await fire_trigger(db, trigger, tenant_id=tenant_id)
                    fired += 1
            except Exception:
                logger.exception(
                    "scheduler tick failed to fire trigger %s for tenant %s", trigger_id, tenant_id
                )
    return fired


async def run_scheduler(*, once: bool = False, interval_seconds: float = 30.0) -> None:
    # Deferred: oc8.channels.poll imports list_active_tenant_ids from this
    # module, so importing it at module scope here would be circular.
    from oc8.channels.poll import poll_tick

    while True:
        try:
            await run_scheduler_tick()
        except Exception:
            logger.exception("unhandled error in scheduler tick")
        try:
            await poll_tick()
        except Exception:
            logger.exception("unhandled error in channel poll tick")
        if once:
            return
        await asyncio.sleep(interval_seconds)
