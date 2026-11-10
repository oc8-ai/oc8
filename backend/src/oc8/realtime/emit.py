"""Thin helpers that both mutate domain state and publish the matching event,
so callers get one call instead of duplicated inline construction."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.realtime.bus import get_event_bus


async def publish_agent_status(agent: m.Agent) -> None:
    await get_event_bus().publish_event(
        agent.tenant_id,
        "agent.status",
        {"agent_id": str(agent.id), "status": agent.status},
        source=f"oc8/agent/{agent.id}",
    )


async def publish_run_output_delta(
    tenant_id: uuid.UUID, *, run_id: uuid.UUID, chunk: str
) -> None:
    """One incremental slice of a run's live output (§8.1 containerized
    runtimes only): the newly-seen text `tail_new_lines()` just diffed out of
    a container's stdout, not the whole cumulative log. Deliberately carries
    no DB write of its own -- unlike `run.status` (persisted on `AgentRun`),
    this transcript exists only for a live viewer and is never replayed from
    a fresh page load, so there is nothing here to persist. Called straight
    from each containerized runtime's poll loop (opencode_runtime/codex_runtime/
    claude_code_runtime's runtime.py), which is why this takes tenant_id/run_id
    as plain values rather than a loaded row -- those plugins hold both already
    and a DB round-trip per poll tick would be wasted work.
    """
    if not chunk:
        return
    await get_event_bus().publish_event(
        tenant_id,
        "run.output_delta",
        {"run_id": str(run_id), "chunk": chunk},
        source=f"oc8/run/{run_id}",
    )


async def publish_copilot_changed(tenant_id: uuid.UUID, *, member_id: uuid.UUID, kind: str) -> None:
    """Ids only: the event bus is tenant-wide, so a colleague's browser receives
    this too -- it must say nothing but "something of member X changed"."""
    await get_event_bus().publish_event(
        tenant_id,
        f"copilot.{kind}",
        {"member_id": str(member_id)},
        source=f"oc8/copilot/{member_id}",
    )


async def publish_builder_changed(
    tenant_id: uuid.UUID,
    *,
    member_id: uuid.UUID,
    proposal_id: uuid.UUID,
    session_id: uuid.UUID | None,
) -> None:
    """A builder proposal's status changed (requested, applied, rejected).
    To the requester only, ids only; the card refetches. Callers publish it
    after their commit (`copilot.builder.announce_builder`)."""
    await get_event_bus().publish_member_event(
        tenant_id,
        [member_id],
        "copilot.agent_builder",
        {"proposal_id": str(proposal_id), "session_id": str(session_id) if session_id else None},
        source=f"oc8/copilot/builder/{proposal_id}",
    )


async def publish_mcp_test_log(
    tenant_id: uuid.UUID, *, connection_id: uuid.UUID, step: str, message: str
) -> None:
    """One line of a "Test connection" run's progress (spawn/handshake/
    list_tools/result), for the connection's own log drawer. Deliberately
    carries no DB write of its own, same reasoning as `publish_run_output_delta`
    above: a fresh page load has nothing to lose by missing these, since the
    connection's persisted `health` field already carries the final verdict.
    `message` must already be operator-safe -- no secret values, only what
    `test_connection` itself would be willing to put in `health.error`.
    """
    await get_event_bus().publish_event(
        tenant_id,
        "mcp.test.log",
        {"connection_id": str(connection_id), "step": step, "message": message},
        source=f"oc8/mcp-connection/{connection_id}",
    )


async def publish_run_token_delta(
    tenant_id: uuid.UUID, *, run_id: uuid.UUID, text: str
) -> None:
    """One text fragment of a model turn as it streams in (Stage 2 token
    streaming), so the Live Log shows an answer forming word by word instead
    of appearing all at once when the turn finishes. Deliberately carries no
    DB write of its own, same as `publish_run_output_delta` above: it is the
    MODEL's own live text, not the container's raw stdout, but the reasoning
    is identical -- the full text still lands durably once the turn
    completes (the transcript entry engine.py/internal_agent.py already
    write), so a page loaded fresh mid-turn has nothing to lose by missing
    these fragments; only an already-open tab benefits from seeing them.
    """
    if not text:
        return
    await get_event_bus().publish_event(
        tenant_id,
        "run.token_delta",
        {"run_id": str(run_id), "text": text},
        source=f"oc8/run/{run_id}",
    )


async def publish_run_reasoning_delta(
    tenant_id: uuid.UUID, *, run_id: uuid.UUID, step: int, text: str
) -> None:
    """One fragment of the model's reasoning as it streams in, for the step
    numbered `step`. The `run.token_delta` of reasoning, with the same
    no-DB-write rule: the step's reasoning lands durably on its `stepTimings`
    entry once the step finishes, so only an already-open tab needs these.
    """
    if not text:
        return
    await get_event_bus().publish_event(
        tenant_id,
        "run.reasoning_delta",
        {"run_id": str(run_id), "step": step, "text": text},
        source=f"oc8/run/{run_id}",
    )


async def publish_run_tool_call(
    tenant_id: uuid.UUID, *, run_id: uuid.UUID, call: dict[str, Any]
) -> None:
    """One tool call the in-process engine just executed (§8.3), so an open
    Live Log tab appends it the instant it happens instead of only seeing
    the whole list once the run ends. Deliberately carries no DB write of
    its own -- unlike `run.output_delta` above, THIS data is already
    durable (the caller persists the same `call` dict via
    `oc8.runtime.run_context.append_tool_call` first), so a page loaded
    fresh mid-run still sees it; this only spares an already-open tab the
    wait for that reload.
    """
    await get_event_bus().publish_event(
        tenant_id,
        "run.tool_call",
        {"run_id": str(run_id), "call": call},
        source=f"oc8/run/{run_id}",
    )


async def publish_run_step_timing(
    tenant_id: uuid.UUID, *, run_id: uuid.UUID, timing: dict[str, Any]
) -> None:
    """One step's finished latency record (`oc8.agent.harness.step_timing`),
    so an open timeline tab fills in that step's duration the moment the next
    step starts instead of only once the whole run ends. Deliberately carries
    no DB write of its own, unlike `publish_run_tool_call` above:
    `stepTimings` has no incremental append path -- both runtimes' own step
    loops already hold this list (in memory for the in-process engine, on
    `run.context` for the isolated runtime) and it lands durably through the
    run's existing terminal/step-boundary context writes; this only spares an
    already-open tab the wait for that reload.
    """
    await get_event_bus().publish_event(
        tenant_id,
        "run.step_timing",
        {"run_id": str(run_id), "timing": timing},
        source=f"oc8/run/{run_id}",
    )


async def record_activity(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    status: str,
    message: str,
    detail: str | None = None,
    cache_hit: bool = False,
) -> m.ActivityEvent:
    row = m.ActivityEvent(
        tenant_id=tenant_id,
        agent_id=agent_id,
        status=status,
        message=message,
        detail=detail,
        cache_hit=cache_hit,
    )
    db.add(row)
    await db.flush()
    await get_event_bus().publish_event(
        tenant_id,
        "activity.logged",
        {
            "id": str(row.id),
            "agent_id": str(agent_id) if agent_id else None,
            "status": status,
            "message": message,
            "detail": detail,
            "cacheHit": cache_hit,
        },
        source=f"oc8/activity/{row.id}",
    )
    return row


async def note_focus(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    focus: str,
    specific: bool = True,
    feed: bool = True,
    cache_hit: bool = False,
    record_url: str | None = None,
) -> None:
    """Record what an agent is working on right now: in the feed, and on the task.

    The feed answers "what happened", and only for whoever is watching. The task
    label answers "what is it doing" from the board, later and to anyone -- which
    is the whole difference for a SCHEDULED agent, whose card otherwise shows the
    trigger's text unchanged on every run: "check the ticket inbox", forever,
    with the ticket it is actually on visible nowhere.

    The label is cosmetic, so nothing here may cost a real action: a task that
    has gone missing is skipped, not raised.
    """
    # `feed=False` is for a refinement of something already logged -- the card
    # gains the record's name, the feed does not gain a duplicate line.
    if feed:
        await record_activity(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            status="info",
            message=focus,
            detail=record_url,
            cache_hit=cache_hit,
        )
    if task_id is None:
        return
    task = await db.get(m.Task, task_id)
    if task is None:
        return
    # Overwritten, not appended: the card answers "what now". A run walks several
    # records and only the current one is worth the two lines a card has.
    #
    # But a VAGUE focus never displaces a specific one. A run searches several
    # times -- for the stages, for the queue -- and any of those landing last used
    # to wipe the ticket it had actually worked: live, 20 of 32 cards read
    # "Durchsucht Ticket", identical on every one and useless. A search still
    # labels a task that has nothing yet, so a run that found an empty queue says
    # so rather than showing a blank card.
    if specific or not task.meta_label:
        task.meta_label = focus
    if record_url:
        payload = dict(task.payload or {})
        payload["record_url"] = record_url
        task.payload = payload
    await db.flush()


_AFTER_COMMIT_KEY = "oc8_after_commit"
#: Live references to the fire-and-forget publish tasks (an unreferenced task
#: can be garbage-collected mid-flight); self-pruning via the done callback.
_after_commit_tasks: set[asyncio.Task[None]] = set()


#: Per open SAVEPOINT, how many events were queued before it began: rolling the
#: savepoint back drops exactly the ones queued inside it.
_SAVEPOINT_MARKS_KEY = "oc8_after_commit_marks"


def _run_pending(session: Any) -> None:
    # `after_commit` fires for a released SAVEPOINT too; only the root
    # transaction's commit makes the rows visible to a reader.
    if session.in_nested_transaction():
        return
    session.info.pop(_SAVEPOINT_MARKS_KEY, None)
    pending = session.info.pop(_AFTER_COMMIT_KEY, [])
    if not pending:
        return
    loop = asyncio.get_running_loop()
    for factory in pending:
        task = loop.create_task(factory())
        _after_commit_tasks.add(task)
        task.add_done_callback(_after_commit_tasks.discard)


def _drop_pending(session: Any, previous_transaction: Any) -> None:
    # `after_soft_rollback` fires for SAVEPOINT rollbacks too. The root
    # transaction's rollback means none of the rows ever existed; a savepoint's
    # means only the ones written inside it did not, so only the events queued
    # inside it go. A savepoint rolled back inside a transaction that then
    # commits keeps every event queued before it.
    if previous_transaction.parent is None:
        session.info.pop(_AFTER_COMMIT_KEY, None)
        session.info.pop(_SAVEPOINT_MARKS_KEY, None)
        return
    marks = session.info.get(_SAVEPOINT_MARKS_KEY, {})
    mark = marks.pop(previous_transaction, None)
    if mark is not None:
        del session.info.get(_AFTER_COMMIT_KEY, [])[mark:]


def publish_after_commit(db: AsyncSession, factory: Callable[[], Awaitable[None]]) -> None:
    """Publish only once the row the event names is visible to a reader.

    `chat.message_created` carries ids, and a browser refetches on it; published
    before the executor's commit, that refetch would come back without the
    message (the executor publishes run.status before it commits, see
    lib/live/apply-event.ts). Dropped on rollback: an event for a row that never
    existed is worse than none -- including one queued inside a SAVEPOINT that
    is rolled back while the transaction around it commits."""
    sync = db.sync_session
    if not sa_event.contains(sync, "after_commit", _run_pending):
        sa_event.listen(sync, "after_commit", _run_pending)
        sa_event.listen(sync, "after_soft_rollback", _drop_pending)
    pending = sync.info.setdefault(_AFTER_COMMIT_KEY, [])
    # Mark every enclosing savepoint on the first event queued inside it. One
    # with no mark yet has had nothing queued inside it, so the current length
    # is exactly where its events begin.
    marks = sync.info.setdefault(_SAVEPOINT_MARKS_KEY, {})
    transaction = sync.get_nested_transaction()
    while transaction is not None and transaction.nested:
        marks.setdefault(transaction, len(pending))
        transaction = transaction.parent
    pending.append(factory)


async def publish_chat_message_created(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    message: m.ChatMessage,
    run_id: uuid.UUID | None,
    acting_subject: str | None = None,
    acting_role: str | None = None,
) -> None:
    """Spec §2.4: `{session_id, message_id, role, run_id}` to every member who
    can open the session -- and still reach its agent -- and to nobody else.
    The audience is resolved now, while the RLS tenant binding still holds;
    the publish waits for the commit."""
    from oc8.chat.audience import session_audience_or_nobody

    audience = await session_audience_or_nobody(
        db, session=session, acting_subject=acting_subject, acting_role=acting_role
    )
    if not audience:
        return
    tenant_id = session.tenant_id
    source = f"oc8/chat/{session.id}"
    data = {
        "session_id": str(session.id),
        "message_id": str(message.id),
        "role": message.role,
        "run_id": str(run_id) if run_id else None,
    }

    async def _publish() -> None:
        await get_event_bus().publish_member_event(
            tenant_id, audience, "chat.message_created", data, source=source
        )

    publish_after_commit(db, _publish)


def publish_chat_session_updated(
    db: AsyncSession, *, session: m.ChatSession, audience: Sequence[uuid.UUID]
) -> None:
    """`{session_id}` to `audience` once `db` commits: the chat's own fields
    (its title) changed, so its row in every list is refetched. Ids only, like
    `chat.message_created`; the audience is resolved by the caller while the
    RLS tenant binding still holds."""
    if not audience:
        return
    tenant_id = session.tenant_id
    members = list(audience)
    data = {"session_id": str(session.id)}
    source = f"oc8/chat/{session.id}"

    async def _publish() -> None:
        await get_event_bus().publish_member_event(
            tenant_id, members, "chat.session_updated", data, source=source
        )

    publish_after_commit(db, _publish)
