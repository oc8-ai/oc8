"""What a person wrote to a chat run that ended before reading it (Ruling 59).

The inputs become ONE new turn of the same chat, by the same person, through
the same door -- staged inside the executor's terminal transaction, never
committed here (Ruling 61): the turn, its queued run and the inputs'
`CARRIED_OVER_STEP` stamp are written together or not at all. The executor
publishes the run after its commit.

A carried-over turn is a turn the person did not send just now, so every
check the door would make NOW is made again first (the person is still a
member, still owns the chat, can still reach the agent, may still start
runs there; on a messenger, still has a live binding and `copilot:manage`;
the secret gate). If any refuses, nothing is started and the inputs stay
unread -- "the run finished before it read this", never "answered as a new
message" without a turn behind it.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.runtime import run_inputs
from oc8.runtime.person import whose_run
from oc8.runtime.run_inputs import (
    CARRIED_OVER_STEP,
    ConsumedInput,
    Delivery,
    cancel_waiting_next_turn_inputs,
    lock_run_inputs,
    unread_run_inputs,
)
from oc8.runtime.states import TERMINAL, RunState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Door:
    originating_operator: str | None
    for_person: str | None
    chat_channel: str | None
    chat_channel_external_id: str | None


async def run_still_open(db: AsyncSession, *, run_id: uuid.UUID) -> bool:
    """The run's state as the database has it, under its row lock: not yet
    ended by anybody else (the reconciler failing it meanwhile)."""
    state = (
        await db.execute(select(m.AgentRun.state).where(m.AgentRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    return state is not None and RunState(state) not in TERMINAL


async def stage_carried_over_turn(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    deliveries: Sequence[Delivery] = ("steer", "next_turn"),
) -> uuid.UUID | None:
    """Stage the turn `run`'s unread inputs of `deliveries` become, joined in
    acceptance order, in the caller's open transaction, and return its queued
    run's id for the caller to publish after committing. None -- and nothing
    written -- when nothing waits, or any check refuses. A DONE run carries
    both deliveries over; a FAILED one only the queued (`next_turn`) inputs
    -- its failure is the answer to what it was steered with.

    The caller holds `lock_run_inputs` (no input can arrive until it
    commits) and has already recorded `run`'s own answer, so the new turn
    comes after it."""
    ctx = run.context or {}
    if run.source != "chat":
        return None
    try:
        session_id = uuid.UUID(str(ctx.get("chat_session_id")))
    except ValueError:
        return None
    rows = await unread_run_inputs(db, run_id=run.id, deliveries=deliveries)
    if not rows:
        return None
    # The checks read in a FRESH session: this one is the run's, and may hold
    # the chat, its owner or her role as they were when the run loaded them
    # (`_resolve_agent_actor`, `_current_chat_sender`) -- an owner offboarded
    # or demoted since would pass on a cached row. Read-only; the writes
    # below stay in the run's terminal transaction.
    async with tenant_session(run.tenant_id) as fresh:
        verdict = await _refusal(fresh, run=run, session_id=session_id, rows=rows)
    if isinstance(verdict, str):
        logger.info("run %s: unread inputs not carried over: %s", run.id, verdict)
        return None
    member_id, door = verdict
    session = (
        await db.execute(
            select(m.ChatSession)
            .where(m.ChatSession.id == session_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    agent = (
        await db.execute(
            select(m.Agent)
            .where(m.Agent.id == run.agent_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if session is None or agent is None:
        return None
    run_id = await _stage_turn(
        db,
        session=session,
        member_id=member_id,
        agent=agent,
        door=door,
        text="\n\n".join(row.text for row in rows),
        anchor_id=_anchor(run),
        # Any queued message the person sent with "Web" on: the joined turn
        # may search. Availability and restricted material are checked again
        # when it does (`web_search`).
        web_search=any(row.web and row.delivery == "next_turn" for row in rows),
    )
    now = dt.datetime.now(tz=dt.UTC)
    for row in rows:
        row.consumed_at = now
        row.consumed_step = CARRIED_OVER_STEP
    await db.flush()
    _announce_after_commit(
        db,
        run=run,
        inputs=[
            ConsumedInput(id=row.id, author_name="", text=row.text, step=CARRIED_OVER_STEP)
            for row in rows
        ],
    )
    return run_id


def _announce_after_commit(
    db: AsyncSession, *, run: m.AgentRun, inputs: list[ConsumedInput]
) -> None:
    """`run.input_consumed` for `inputs`, once the transaction commits, so
    every open composer drops them from the waiting list."""
    from oc8.realtime.emit import publish_after_commit

    if inputs:
        publish_after_commit(
            db,
            partial(
                # Looked up now, not at import: the module's own function.
                run_inputs.publish_inputs_consumed,
                run.tenant_id,
                run_id=run.id,
                consumed=inputs,
            ),
        )


async def end_queued_inputs(db: AsyncSession, *, run: m.AgentRun, carry_over: bool) -> None:
    """The queued (`next_turn`) inputs of a run that ends anywhere but the
    executor's main terminal branch: a crash, the reconciler, a lost lease, a
    run that never started. Call before the run's terminal transition, in
    that transaction -- it takes `lock_run_inputs` (after the tenant's audit
    lock, before any chat_session row), so no input arrives until it commits.

    `carry_over` (a FAILED end whose next turn can run): they become the
    chat's next turn, its run published after commit; if a check refuses,
    they stay unread like a failed run's steer inputs. Otherwise (a stopped
    end, or one whose next turn could not run either): cancelled,
    `CANCELLED_STEP`. Either way the composer is told after commit. A
    carry-over that raises is rolled back to its savepoint and leaves them
    unread: it never fails the end of the run. Nothing for a run that is not
    a chat turn: only a chat turn takes a queued input (`add_run_input`)."""
    if run.source != "chat":
        return
    await lock_run_inputs(db, tenant_id=run.tenant_id, run_id=run.id)
    if carry_over:
        from oc8.realtime.emit import publish_after_commit
        from oc8.runtime.intake import publish_run

        try:
            async with db.begin_nested():
                carried = await stage_carried_over_turn(db, run=run, deliveries=("next_turn",))
        except Exception:
            logger.exception("run %s: queued inputs not carried over", run.id)
            return
        if carried is not None:
            publish_after_commit(db, partial(publish_run, run_id=carried, tenant_id=run.tenant_id))
        return
    cancelled = await cancel_waiting_next_turn_inputs(db, run_id=run.id)
    _announce_after_commit(db, run=run, inputs=cancelled)


def _anchor(run: m.AgentRun) -> uuid.UUID | None:
    """The user turn `run` answered, from its context."""
    raw = (run.context or {}).get("reply_to_message_id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def _refusal(
    db: AsyncSession, *, run: m.AgentRun, session_id: uuid.UUID, rows: list[m.RunInput]
) -> str | tuple[uuid.UUID, _Door]:
    """The door's checks, made now, in `db` -- a session of their own, so
    every row is read as it is now. A reason when refused, else the owner's
    member id and the door the turn goes through."""
    from oc8.authz.authority import authority_for_member, member_holds_assigned_permission
    from oc8.authz.permissions import COPILOT, MANAGE, RUN_START, perm
    from oc8.chat.reach import member_can_reach_agent
    from oc8.copilot.followups import live_binding_external_id
    from oc8.copilot.redaction import carries_secret_value
    from oc8.tenants.activity import tenant_may_run

    ctx = run.context or {}
    session = await db.get(m.ChatSession, session_id)
    if session is None or session.tenant_id != run.tenant_id:
        return "the chat is gone"
    authors = {row.author_member_id for row in rows}
    if authors != {session.member_id}:
        return "not written by the chat's owner"
    member = await db.get(m.OrgMember, session.member_id)
    if member is None or member.deleted_at is not None or member.tenant_id != run.tenant_id:
        return "the chat's owner is no longer a member"
    # Whose run it is NOW, with the account it acts through (`whose_run`):
    # never a turn under somebody else's account.
    if not (await whose_run(db, run)).is_own(member.subject):
        return "the run is not the chat owner's"
    agent = await db.get(m.Agent, run.agent_id)
    if agent is None or agent.id != session.agent_id:
        return "the agent is gone"
    if not await tenant_may_run(run.tenant_id):
        return "the organisation may not run"
    if agent.is_tenant_assistant and carries_secret_value("\n\n".join(r.text for r in rows)):
        return "the secret gate"
    channel = ctx.get("chat_channel")
    if channel and ctx.get("chat_channel_external_id"):
        # The messenger door (`bind_from_free_text`): a live binding of THIS
        # member on that channel -- whose current external id is where the
        # turn goes, never the one stored on the run -- and copilot:manage.
        external_id = await live_binding_external_id(
            db, tenant_id=run.tenant_id, member_id=member.id, channel=str(channel)
        )
        if not external_id:
            return "no live binding on the channel"
        if not await member_holds_assigned_permission(
            db, member=member, permission=perm(COPILOT, MANAGE)
        ):
            return "no copilot:manage"
        return (
            member.id,
            _Door(
                originating_operator=None,
                for_person=member.subject,
                chat_channel=str(channel),
                chat_channel_external_id=external_id,
            ),
        )
    # The web door (POST /chat/sessions/{id}/messages): reach the agent, and
    # RUN_START unless it is the Assistant -- under the member's stored rights
    # and the token role only if this run recorded HER live one (Ruling 33),
    # else none: fail closed.
    raw_role = ctx.get("acting_token_role")
    token_role = str(raw_role) if isinstance(raw_role, str) and raw_role else None
    if not await member_can_reach_agent(
        db, member=member, agent_id=agent.id, token_role=token_role
    ):
        return "the owner can no longer reach the agent"
    if not agent.is_tenant_assistant:
        authority = await authority_for_member(db, member, token_role=token_role)
        if RUN_START not in authority.tenant_wide:
            return "the owner may not start runs"
    return (
        member.id,
        _Door(
            originating_operator=member.subject,
            for_person=None,
            chat_channel=None,
            chat_channel_external_id=None,
        ),
    )


async def _stage_turn(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    member_id: uuid.UUID,
    agent: m.Agent,
    door: _Door,
    text: str,
    anchor_id: uuid.UUID | None,
    web_search: bool = False,
) -> uuid.UUID:
    """`send_message`'s turn, without its commit: the user message, the run
    answering it (queued, unpublished) and its announcement after commit. No
    context manifest: that needs the member's live token role.

    Placed as the ended run's write (`chat.tree.append_run_write`), anchored on
    the turn that run answered: it follows that run's answer, and moves the
    leaf only while the person is still on that branch."""
    from oc8.chat import tree
    from oc8.chat.modes import parse_command
    from oc8.chat.service import WEB_SEARCH_CONTEXT_KEY, _build_turn_context, _kb_reference_text
    from oc8.realtime.emit import publish_chat_message_created
    from oc8.runtime.repository import RunRepository

    mode, body = parse_command(text)
    message = m.ChatMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        role="user",
        content=body,
        mode=mode.key if mode is not None else None,
        context_refs=[],
        author_member_id=member_id,
    )
    await tree.append_run_write(db, session=session, message=message, anchor_id=anchor_id)
    history = await tree.path_before(db, session=session, message=message)
    context = _build_turn_context(
        session=session,
        turn_message=body,
        turn_text=body + _kb_reference_text([], mode, None),
        turn_role="user",
        transcript_until=history[-1].id if history else None,
        reply_to_message_id=message.id,
        image_attachments=[],
        originating_operator=door.originating_operator,
        operator_role=None,
        chat_channel=door.chat_channel,
        chat_channel_external_id=door.chat_channel_external_id,
        role="user",
        mode=mode,
        for_person=door.for_person,
        resolved_refs=[],
        extra_context=None,
    )
    if web_search:
        # As `send_message` sets it: never through `extra_context`.
        context[WEB_SEARCH_CONTEXT_KEY] = True
    if session.task_id is None:
        from oc8.agent.engine import open_run_task

        task = await open_run_task(
            db, agent=agent, task_text=str(context["task"]), tenant_id=session.tenant_id
        )
        session.task_id = task.id
        await db.flush()
    run = await RunRepository(db).create(
        tenant_id=session.tenant_id,
        agent_id=session.agent_id,
        context=context,
        source="chat",
        task_id=session.task_id,
    )
    await publish_chat_message_created(
        db,
        session=session,
        message=message,
        run_id=run.id,
        acting_subject=door.originating_operator,
    )
    return run.id
