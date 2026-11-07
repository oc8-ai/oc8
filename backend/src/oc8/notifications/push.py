# backend/src/oc8/notifications/push.py
"""Sends Web Push notifications to an operator's subscribed browsers.

`send_push_for_tenant` never raises: it is called from a fire-and-forget
background task (see `oc8.realtime.bus.EventBus`), and a slow or failing
push service must never affect the event that triggered it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from pywebpush import WebPushException, webpush_async
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings

logger = logging.getLogger(__name__)

#: How long the push service may hold an undelivered message (RFC 8030 TTL).
#: pywebpush defaults to 0, which means "deliver right now or throw it away" --
#: the exact opposite of this feature's point, which is to reach an operator
#: whose browser is closed. A day is long enough for an overnight approval and
#: short enough that nobody is woken by yesterday's news.
_PUSH_TTL_SECONDS = 86400
#: Per-request network timeout. Without one aiohttp falls back to its 5-minute
#: session default, and `send_push_for_tenant`'s caller holds a pooled DB
#: connection open for the whole `asyncio.gather` -- so one hung push service
#: could park that connection for five minutes per approval.
_PUSH_TIMEOUT_SECONDS = 10


async def send_push_for_tenant(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: dict[str, Any],
    member_id: uuid.UUID | None = None,
) -> None:
    """Push `payload` to the tenant's subscribed browsers -- or, with
    `member_id`, to that member's alone (a member's own approval, D3a)."""
    settings = get_settings()
    if not settings.vapid_public_key or not settings.vapid_private_key:
        return
    query = select(m.PushSubscription).where(m.PushSubscription.tenant_id == tenant_id)
    if member_id is not None:
        query = query.where(m.PushSubscription.member_id == member_id)
    result = await db.execute(query)
    subs = list(result.scalars().all())
    if not subs:
        return
    # `_send_one` does the concurrent, I/O-bound network call ONLY -- it must
    # not touch `db` itself. AsyncSession is not safe for concurrent use, and
    # `asyncio.gather` schedules every `_send_one` call on the same session at
    # once; two subscriptions both needing a delete+commit at the same moment
    # would race on that shared session and raise (SQLAlchemy's own docs: "not
    # safe for use in concurrent tasks"). So `_send_one` only *reports* which
    # subscriptions are dead, and the deletes/commit happen here, sequentially,
    # after every concurrent send has finished.
    to_prune = await asyncio.gather(*(_send_one(sub, payload) for sub in subs))
    dead = [sub for sub in to_prune if sub is not None]
    if dead:
        for sub in dead:
            await db.delete(sub)
        await db.commit()


async def _send_one(sub: m.PushSubscription, payload: dict[str, Any]) -> m.PushSubscription | None:
    """Send to one subscription. Returns `sub` if it should be pruned (dead
    endpoint), else None. Never touches the database and never raises --
    see the concurrency note on `send_push_for_tenant`."""
    settings = get_settings()
    try:
        await webpush_async(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
            ttl=_PUSH_TTL_SECONDS,
            timeout=_PUSH_TIMEOUT_SECONDS,
        )
    except WebPushException as exc:
        # `.response` is an `aiohttp.ClientResponse` here (webpush_async's own
        # shape), whose status field is `.status` -- NOT `.status_code`, which
        # only the synchronous `webpush()`'s `requests.Response` carries.
        push_status = getattr(exc.response, "status", None)
        if push_status in (404, 410):
            return sub
        logger.warning("push send failed (endpoint=%s): %s", sub.endpoint, exc)
    except Exception:
        logger.warning("push send failed (endpoint=%s)", sub.endpoint, exc_info=True)
    return None
