"""Fan out a dispatched inbound event to matching event-kind Trigger rows,
across every tenant -- mirrors the cron scheduler's tenant-discovery pattern
since RLS keeps each tenant's Trigger rows invisible outside its own bound
session. Registered once, as a single wildcard handler, at app startup."""

from __future__ import annotations

import logging

from sqlalchemy import select

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.events.types import InboundEvent
from oc8.tenants.activity import TenantInactive
from oc8.triggers.scheduler import fire_trigger, list_runnable_tenant_ids

logger = logging.getLogger(__name__)


async def handle_inbound_event(event: InboundEvent) -> None:
    """Each fire opens its own tenant_session -- see scheduler.run_scheduler_tick's
    docstring for why sharing one session across multiple fires is unsafe
    (fire_trigger()'s commit drops the transaction-local tenant binding)."""
    for tenant_id in await list_runnable_tenant_ids():
        try:
            async with tenant_session(tenant_id) as db:
                result = await db.execute(
                    select(m.Trigger.id).where(
                        m.Trigger.kind == "event",
                        m.Trigger.enabled.is_(True),
                        m.Trigger.event_source == event.source,
                        m.Trigger.event_type == event.type,
                    )
                )
                match_ids = list(result.scalars().all())
        except Exception:
            logger.exception("event handling failed to list matches for tenant %s", tenant_id)
            continue
        for trigger_id in match_ids:
            try:
                async with tenant_session(tenant_id) as db:
                    trigger = await db.get(m.Trigger, trigger_id)
                    if trigger is None:
                        continue
                    await fire_trigger(
                        db,
                        trigger,
                        tenant_id=tenant_id,
                        idempotency_key=(
                            f"{event.source}:{event.delivery_id}:{trigger_id}"
                            if event.delivery_id
                            else None
                        ),
                    )
            except TenantInactive:
                # The organisation stopped being allowed to run after it was
                # listed. Expected, not an error.
                logger.info(
                    "event handling skipped trigger %s: organisation %s is not active",
                    trigger_id,
                    tenant_id,
                )
            except Exception:
                logger.exception(
                    "event handling failed to fire trigger %s for tenant %s", trigger_id, tenant_id
                )
