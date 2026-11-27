"""Temporary chats (spec §5.6): a conversation that is not kept.

A temporary chat is an ordinary chat row with `ChatSession.temporary` set.
Everything that would keep something of it checks the flag: the list routes,
the title, the background checkpoint, feedback, pin, project and export
(`api/v1/chat*.py`), and the edition chat port. Its runs carry
`TEMPORARY_CHAT_KEY` in their context (`chat.service._build_turn_context`),
so the runtime offers and dispatches no `memory_write` for them
(`agent.control_tools`).

The chat is deleted when the person leaves it (the frontend calls the
ordinary DELETE) and, for a tab that was simply closed, by
`purge_all_temporary_chats` on the worker's housekeeping tick once it has been
idle for `PURGE_AFTER`. Both take `chat.service.delete_session`, so its
messages, files and chat knowledge base go with it. Runs, approvals, audit
events and usage records are governance and stay.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m

logger = logging.getLogger(__name__)

#: The run-context key a temporary chat's turn carries.
TEMPORARY_CHAT_KEY: Final = "temporary_chat"

#: Idle time after which the purge deletes a temporary chat nobody left.
PURGE_AFTER: Final = dt.timedelta(hours=24)

#: What `memory_write` answers in a temporary chat's run.
NO_MEMORY_REFUSAL: Final = "a temporary chat keeps no notes"


def is_temporary_run(context: Mapping[str, Any] | None) -> bool:
    """Whether a run answers a turn of a temporary chat."""
    return bool((context or {}).get(TEMPORARY_CHAT_KEY))


async def purge_temporary_chats(db: AsyncSession, *, older_than: dt.timedelta = PURGE_AFTER) -> int:
    """Delete the tenant's temporary chats idle for longer than `older_than`
    (last message, or creation when it has none). Each goes through
    `delete_session` in its own savepoint, so one that fails stays for the
    next tick and does not keep the others. Returns how many were deleted.
    Flush-only; the caller commits."""
    from oc8.chat.service import delete_session

    cutoff = dt.datetime.now(dt.UTC) - older_than
    stale = list(
        (
            await db.execute(
                select(m.ChatSession).where(
                    m.ChatSession.temporary.is_(True),
                    func.coalesce(m.ChatSession.last_message_at, m.ChatSession.created_at) < cutoff,
                )
            )
        ).scalars()
    )
    deleted = 0
    for session in stale:
        try:
            async with db.begin_nested():
                await delete_session(db, tenant_id=session.tenant_id, session=session)
        except Exception:
            logger.warning("temporary chat %s: purge failed", session.id, exc_info=True)
            continue
        deleted += 1
    return deleted


async def purge_all_temporary_chats() -> int:
    """`purge_temporary_chats` in every tenant: one tick of the worker's
    housekeeping timer. Never raises."""
    from oc8.db.session import tenant_session
    from oc8.triggers.scheduler import list_active_tenant_ids

    try:
        tenants = await list_active_tenant_ids()
    except Exception:
        logger.warning("temporary chats: could not list tenants; skipping purge", exc_info=True)
        return 0
    deleted = 0
    for tenant_id in tenants:
        try:
            async with tenant_session(tenant_id) as db:
                deleted += await purge_temporary_chats(db)
        except Exception:
            logger.warning("temporary chats: purge failed for tenant %s", tenant_id, exc_info=True)
    return deleted
