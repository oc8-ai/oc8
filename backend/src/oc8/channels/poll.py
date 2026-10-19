"""Poll-based ingress for an approval channel with no public webhook URL to
receive on (§5.6 addendum) -- e.g. `telegram_approvals` running on a
developer's machine with no tunnel, where Telegram can never reach
`POST /channels/{channel}/webhook/{tenant_id}`.

A channel opts in by implementing an optional `poll(*, offset) -> (updates,
next_offset)` method (duck-typed, like `ApprovalChannel.say` already is) --
core does not otherwise know or care which channels support this. Every
update it returns is handed to the SAME `oc8.channels.dispatch.process_inbound`
the webhook route calls, so an update is handled identically whichever way
it arrived.

Every database access below opens its OWN `tenant_session` rather than
sharing one across the tick. `tenant_session` binds `app.tenant_id` with
`set_config(..., is_local=true)` -- transaction-local -- and
`process_inbound` (via `bind_from_link`/`apply_decision`) already commits
or rolls back per update it processes. Sharing a session across multiple
updates would silently unbind RLS after the first one, the same failure
mode `oc8.mail.send.send_mail` was bitten by; this mirrors
`triggers.scheduler.run_scheduler_tick`'s own one-`tenant_session`-per-fire
shape instead.

Not a general-purpose distributed job: this assumes a single scheduler
replica. Telegram's own `getUpdates` offset semantics already make a stray
double-poll harmless (it just returns nothing new), so no additional
locking is added for the case this exists to solve -- a local dev machine.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from oc8 import models as m
from oc8.channels.dispatch import process_inbound
from oc8.channels.registry import channels_for_tenant
from oc8.db.session import tenant_session
from oc8.triggers.scheduler import list_runnable_tenant_ids

logger = logging.getLogger(__name__)


async def poll_tick() -> int:
    """One tick: for every tenant that may run, poll every channel that
    supports it. Returns the number of updates processed. Never raises -- a tenant
    or channel that fails is logged and skipped, the same isolation
    `run_scheduler_tick` already gives cron triggers."""
    processed = 0
    for tenant_id in await list_runnable_tenant_ids():
        try:
            processed += await _poll_tenant(tenant_id)
        except Exception:
            logger.exception("poll tick failed for tenant %s", tenant_id)
    return processed


async def _poll_tenant(tenant_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as db:
        channels = await channels_for_tenant(db, tenant_id=tenant_id)
    # The built ApprovalChannel instances hold no db reference (just an
    # http client and config), so they're safe to keep and call after this
    # short, read-only session has already closed.

    processed = 0
    for channel_id, impl in channels.items():
        poll = getattr(impl, "poll", None)
        if poll is None:
            continue  # this channel is webhook-only; nothing to do here
        try:
            processed += await _poll_channel(tenant_id, channel_id, impl, poll)
        except Exception:
            logger.exception("poll failed for channel %s, tenant %s", channel_id, tenant_id)
    return processed


async def _cursor_offset(tenant_id: uuid.UUID, channel_id: str) -> int:
    async with tenant_session(tenant_id) as db:
        cursor = (
            await db.execute(
                select(m.ChannelPollCursor).where(
                    m.ChannelPollCursor.tenant_id == tenant_id,
                    m.ChannelPollCursor.channel == channel_id,
                )
            )
        ).scalar_one_or_none()
        return cursor.last_update_id if cursor is not None else 0


async def _advance_cursor(tenant_id: uuid.UUID, channel_id: str, next_offset: int) -> None:
    async with tenant_session(tenant_id) as db:
        cursor = (
            await db.execute(
                select(m.ChannelPollCursor).where(
                    m.ChannelPollCursor.tenant_id == tenant_id,
                    m.ChannelPollCursor.channel == channel_id,
                )
            )
        ).scalar_one_or_none()
        if cursor is None:
            db.add(
                m.ChannelPollCursor(
                    tenant_id=tenant_id, channel=channel_id, last_update_id=next_offset
                )
            )
        else:
            cursor.last_update_id = next_offset


async def _poll_channel(tenant_id: uuid.UUID, channel_id: str, impl: Any, poll: Any) -> int:
    offset = await _cursor_offset(tenant_id, channel_id)
    updates, next_offset = await poll(offset=offset)

    for update in updates:
        if not isinstance(update, dict):
            continue
        async with tenant_session(tenant_id) as db:
            await process_inbound(db, impl, tenant_id=tenant_id, channel=channel_id, update=update)

    if next_offset != offset:
        await _advance_cursor(tenant_id, channel_id, next_offset)
    return len(updates)
