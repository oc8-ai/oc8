"""Thin helpers that both mutate domain state and publish the matching event,
so callers get one call instead of duplicated inline construction."""

from __future__ import annotations

import uuid
from typing import Any

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
    await db.flush()
