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
import uuid
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.realtime.emit import publish_after_commit
from oc8.runtime.states import TERMINAL, RunState

if TYPE_CHECKING:
    from oc8.auth.principal import Principal

logger = logging.getLogger(__name__)

#: The run-context key a temporary chat's turn carries.
TEMPORARY_CHAT_KEY: Final = "temporary_chat"

#: Idle time after which the purge deletes a temporary chat nobody left.
PURGE_AFTER: Final = dt.timedelta(hours=24)

#: What `memory_write` answers in a temporary chat's run.
NO_MEMORY_REFUSAL: Final = "a temporary chat keeps no notes"


def is_temporary_run(context: Mapping[str, Any] | None) -> bool:
    """Whether a run's context says it works for a temporary chat. The fast
    check: the key rides along on delegations and wake-ups. Where a decision
    must not depend on a key being carried, use `in_temporary_chat`."""
    return bool((context or {}).get(TEMPORARY_CHAT_KEY))


async def in_temporary_chat(db: AsyncSession, context: Mapping[str, Any] | None) -> bool:
    """Whether a run works for a temporary chat: its context says so, or the
    chat its `chat_session_id` names is one. Every run of a chat -- a turn, a
    delegated sub-run, a wake-up, a sub-sub-run -- carries that id, so a hop
    that drops the key is still recognised. One indexed lookup."""
    if is_temporary_run(context):
        return True
    raw = (context or {}).get("chat_session_id")
    if not raw:
        return False
    try:
        session_id = uuid.UUID(str(raw))
    except ValueError:
        return False
    temporary = await db.scalar(
        select(m.ChatSession.temporary).where(m.ChatSession.id == session_id)
    )
    return bool(temporary)


#: Temporary chats one purge tick looks at per tenant; the rest wait a tick.
PURGE_BATCH: Final = 200

#: Run states in which a run may be taking a step right now, or is about to:
#: a stop reaches it only at its next step boundary or its start.
_MOVING: Final = (RunState.QUEUED.value, RunState.RUNNING.value)


async def _unfinished_runs(db: AsyncSession, *, session: m.ChatSession) -> list[m.AgentRun]:
    """Every run working for this chat that has not finished: its turns, and
    the delegated sub-runs and wake-ups that carry its id."""
    return list(
        (
            await db.execute(
                select(m.AgentRun)
                .where(
                    m.AgentRun.tenant_id == session.tenant_id,
                    m.AgentRun.context["chat_session_id"].astext == str(session.id),
                    m.AgentRun.state.notin_([s.value for s in TERMINAL]),
                )
                .order_by(m.AgentRun.created_at.asc())
            )
        ).scalars()
    )


async def stop_chat_runs(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    actor_id: uuid.UUID | None,
    principal: Principal | None,
) -> list[m.AgentRun]:
    """Stop every unfinished run of the chat through the cancel a Stop
    records (`runtime.cancellation.record_operator_cancel`), once per run: a
    run parked on an approval is woken, its cards expire and the worker ends
    it before it starts again; a queued run is skipped at pickup; a running
    run stops at its next step boundary. A run waiting for an answer is
    re-queued the same way, as an answer would, so the before-start check
    ends it instead of leaving it waiting on a chat that is gone. Returns the
    runs stopped, in their state after the stop. Flush-only."""
    from oc8.runtime.cancellation import record_operator_cancel
    from oc8.runtime.intake import publish_run
    from oc8.runtime.repository import RunRepository

    runs = await _unfinished_runs(db, session=session)
    for run in runs:
        already = await db.scalar(
            select(m.RunCancellation.id).where(m.RunCancellation.run_id == run.id)
        )
        if already is None:
            await record_operator_cancel(
                db,
                run=run,
                principal=principal,
                actor_id=actor_id,
                resource_extra={
                    "door": "temporary_chat",
                    "chat_session_id": str(session.id),
                    "agent_id": str(run.agent_id),
                },
            )
        fresh = await db.get(m.AgentRun, run.id, populate_existing=True)
        if fresh is not None and await RunRepository(db).transition_if(
            fresh, RunState.WAITING_FOR_INPUT, RunState.QUEUED
        ):
            run_id, tenant_id = fresh.id, fresh.tenant_id
            publish_after_commit(db, partial(publish_run, run_id=run_id, tenant_id=tenant_id))
        if fresh is not None:
            await db.refresh(fresh)
    return runs


async def cancel_queued_inputs(db: AsyncSession, *, runs: list[m.AgentRun]) -> None:
    """End the messages queued behind `runs` as cancelled (stop means stop):
    none becomes a turn of a chat that is gone. Takes each run's input lock,
    so after the knowledge and file rows of a delete (`chat.tree`)."""
    from oc8.runtime.run_inputs import cancel_waiting_next_turn_inputs, lock_run_inputs

    for run in runs:
        await lock_run_inputs(db, tenant_id=run.tenant_id, run_id=run.id)
        await cancel_waiting_next_turn_inputs(db, run_id=run.id)


async def purge_temporary_chats(
    db: AsyncSession, *, tenant_id: uuid.UUID, older_than: dt.timedelta = PURGE_AFTER
) -> int:
    """Delete the tenant's temporary chats idle for longer than `older_than`
    (last message, or creation when it has none), at most `PURGE_BATCH` per
    call. Each goes through `delete_session` in its own savepoint, so one
    that fails stays for the next tick and does not keep the others.

    A chat with a run that is queued or running is not deleted on this tick:
    the run is stopped (asynchronously -- it ends at its next step or its
    start) and the chat is deleted on a later tick, once nothing of it is
    moving. A run parked on an approval or a question is woken by that stop
    and so counts as moving until the worker has ended it. Returns how many
    were deleted. Flush-only; the caller commits."""
    from oc8.chat.service import delete_session

    cutoff = dt.datetime.now(dt.UTC) - older_than
    stale = list(
        (
            await db.execute(
                select(m.ChatSession)
                .where(
                    m.ChatSession.tenant_id == tenant_id,
                    m.ChatSession.temporary.is_(True),
                    func.coalesce(m.ChatSession.last_message_at, m.ChatSession.created_at) < cutoff,
                )
                .order_by(func.coalesce(m.ChatSession.last_message_at, m.ChatSession.created_at))
                .limit(PURGE_BATCH)
            )
        ).scalars()
    )
    deleted = 0
    for session in stale:
        try:
            async with db.begin_nested():
                stopped = await stop_chat_runs(db, session=session, actor_id=None, principal=None)
                if any(run.state in _MOVING for run in stopped):
                    logger.info(
                        "temporary chat %s: runs stopping; deleted on a later tick", session.id
                    )
                    continue
                await delete_session(db, tenant_id=tenant_id, session=session)
        except Exception:
            logger.warning("temporary chat %s: purge failed", session.id, exc_info=True)
            continue
        deleted += 1
    return deleted


async def purge_all_temporary_chats() -> int:
    """`purge_temporary_chats` in every tenant, inactive ones included (a
    cleanup must visit every organisation, `list_active_tenant_ids`): one
    tick of the worker's housekeeping timer. Never raises."""
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
                deleted += await purge_temporary_chats(db, tenant_id=tenant_id)
        except Exception:
            logger.warning("temporary chats: purge failed for tenant %s", tenant_id, exc_info=True)
    return deleted
