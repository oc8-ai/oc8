"""Direct chat with a single agent.

Deliberately NOT a parallel execution engine: every user message becomes a
real `AgentRun` (`source="chat"`), enqueued through the exact same
`oc8.runtime.intake.enqueue_run` funnel every other run source uses. That
means a chat turn gets the department frame's guardrails, tool policy and
approval-suspend/resume for free -- a `delete_record` call typed into chat
pauses in "My work" exactly like one from an autonomous run, because as far
as the executor is concerned it IS one.

All turns in one `ChatSession` share a single `Task` (`ChatSession.task_id`,
opened by `send_message` itself on turn one and passed into every
`enqueue_run` call, that one included) -- see `engine.open_run_task`'s
`resume_task_id` handling, which is what makes passing an already-set
`AgentRun.task_id` reuse that task instead of opening a new one. One chat
session is one item on the department board, not one per message.

`record_assistant_reply` is called from `oc8.runtime.executor` the moment a
`source="chat"` run reaches a terminal state -- it is the one place a run's
outcome becomes the durable transcript message the Chat UI reads.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Literal

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.modes import mode_directive, parse_command
from oc8.copilot.redaction import is_secret_request, redact_text
from oc8.runtime.intake import enqueue_run
from oc8.storage import s3

logger = logging.getLogger(__name__)

#: How many prior turns to fold into the next run's `task` text. A chat is
#: interactive, not a long autonomous investigation -- this keeps the prompt
#: bounded without needing a separate summarization step for v1.
_TRANSCRIPT_TURNS = 20

#: Matches the retired `oc8.copilot.chat._SECRET_REFUSAL` text -- carried over
#: rather than imported so this module has no dependency on the module Task 5
#: deletes.
_SECRET_REFUSAL = (
    "I cannot accept, request, or use credential values. Use the normal secret setup flow."
)


async def create_session(
    db: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, member_id: uuid.UUID
) -> m.ChatSession:
    session = m.ChatSession(tenant_id=tenant_id, agent_id=agent_id, member_id=member_id)
    db.add(session)
    await db.flush()
    return session


async def get_session(
    db: AsyncSession, *, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> m.ChatSession | None:
    return (
        await db.execute(
            select(m.ChatSession).where(
                m.ChatSession.tenant_id == tenant_id, m.ChatSession.id == session_id
            )
        )
    ).scalar_one_or_none()


async def list_sessions(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
) -> list[m.ChatSession]:
    stmt = select(m.ChatSession).where(
        m.ChatSession.tenant_id == tenant_id, m.ChatSession.member_id == member_id
    )
    if agent_id is not None:
        stmt = stmt.where(m.ChatSession.agent_id == agent_id)
    stmt = stmt.order_by(m.ChatSession.last_message_at.desc().nulls_last(), m.ChatSession.id.desc())
    return list((await db.execute(stmt)).scalars())


async def rename_session(db: AsyncSession, *, session: m.ChatSession, title: str) -> None:
    session.title = title
    await db.flush()


async def delete_session(db: AsyncSession, *, tenant_id: uuid.UUID, session: m.ChatSession) -> None:
    """`ChatMessage` carries no DB-level foreign key to `ChatSession` (this
    codebase's tables generally don't use them) -- its transcript has to be
    removed explicitly or it survives as orphaned rows.

    Same for the turns' file attachments: the design doc says a deleted
    session's attachments are "cleaned up the same way (DB row + object-store
    blob)", and nothing else ever would -- an attachment's `owner_id` points
    at a `ChatSession` before its message is sent and at a `ChatMessage`
    after, so once both are gone the row is unreachable through every
    endpoint and the blob is billable storage no one can name. Includes
    attachments still owned by the session itself: uploaded, then never sent
    (the operator removed the chip, or closed the tab).

    The object-store deletes are best-effort by design -- a blob the store no
    longer has (or a store that is briefly down) must not block deleting the
    conversation, which is the thing the operator actually asked for. A leaked
    blob is recoverable later from the bucket; a chat that refuses to delete
    is not.
    """
    message_ids = list(
        (
            await db.execute(
                select(m.ChatMessage.id).where(
                    m.ChatMessage.tenant_id == tenant_id,
                    m.ChatMessage.session_id == session.id,
                )
            )
        ).scalars()
    )
    attachments = list(
        (
            await db.execute(
                select(m.FileAttachment).where(
                    m.FileAttachment.tenant_id == tenant_id,
                    m.FileAttachment.owner_type == "chat_message",
                    m.FileAttachment.owner_id.in_([session.id, *message_ids]),
                )
            )
        ).scalars()
    )
    for attachment in attachments:
        try:
            await s3.delete_object(attachment.bucket_key)
        except Exception:  # best-effort by design -- see the docstring
            logger.warning(
                "could not delete attachment blob %s for chat session %s",
                attachment.bucket_key,
                session.id,
            )
        await db.delete(attachment)
    await db.execute(
        delete(m.ChatMessage).where(
            m.ChatMessage.tenant_id == tenant_id, m.ChatMessage.session_id == session.id
        )
    )
    await db.delete(session)


async def list_messages(
    db: AsyncSession, *, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[m.ChatMessage]:
    stmt = (
        select(m.ChatMessage)
        .where(m.ChatMessage.tenant_id == tenant_id, m.ChatMessage.session_id == session_id)
        .order_by(m.ChatMessage.id.asc())
    )
    return list((await db.execute(stmt)).scalars())


def _speaker(role: str) -> str:
    if role == "user":
        return "User"
    if role == "followup":
        return "Follow-up (scheduled by you, not typed by the user)"
    return "You (assistant)"


def _build_task_text(history: list[m.ChatMessage], new_message: str, new_role: str = "user") -> str:
    """The run's `task` text: the whole visible conversation, plain-labelled,
    ending in the new turn. `run_agent`/`internal_agent.py` know nothing of
    "chat" -- from their side this is just one more task instruction, so any
    continuity across turns has to be spelled out here rather than relying
    on native multi-turn state that does not exist on that path."""
    lines = []
    for msg in history[-_TRANSCRIPT_TURNS:]:
        lines.append(f"{_speaker(msg.role)}: {msg.content}")
    lines.append(f"{_speaker(new_role)}: {new_message}")
    return "\n".join(lines)


async def _resolve_context_refs(
    db: AsyncSession, *, tenant_id: uuid.UUID, refs: list[dict[str, str]] | None
) -> list[dict[str, str]]:
    """The `#` references that actually exist, with their labels.

    Anything unresolvable is DROPPED, not passed on: an id naming another
    tenant's knowledge base (RLS makes it invisible here), a soft-deleted one,
    or a `kind` this version does not implement. A dropped reference is visible
    -- the chip is gone from the sent message -- which is the honest outcome;
    the alternative is a turn instructed to consult something nobody can read.

    `knowledge_base` is the only kind in Phase 1. A document or an external
    record is a bigger question (a record reference has to name a connection
    too) and belongs with the room work.
    """
    if not refs:
        return []
    wanted: list[uuid.UUID] = []
    for ref in refs:
        if ref.get("kind") != "knowledge_base":
            continue
        try:
            wanted.append(uuid.UUID(str(ref.get("id"))))
        except (ValueError, TypeError):
            continue
    if not wanted:
        return []
    rows = (
        await db.execute(
            select(m.KnowledgeBase).where(
                m.KnowledgeBase.tenant_id == tenant_id,
                m.KnowledgeBase.id.in_(wanted),
                m.KnowledgeBase.deleted_at.is_(None),
            )
        )
    ).scalars()
    by_id = {kb.id: kb for kb in rows}
    # Caller order preserved, so the chips read back in the order they were
    # attached rather than in whatever order the database returned.
    return [
        {"kind": "knowledge_base", "id": str(kb_id), "label": by_id[kb_id].name}
        for kb_id in wanted
        if kb_id in by_id
    ]


async def send_message(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    tenant_id: uuid.UUID,
    message: str,
    attachment_ids: list[uuid.UUID] | None = None,
    context_refs: list[dict[str, str]] | None = None,
    originating_operator: str | None,
    operator_role: str | None = None,
    chat_channel: str | None = None,
    chat_channel_external_id: str | None = None,
    role: Literal["user", "followup"] = "user",
    extra_context: dict[str, object] | None = None,
) -> tuple[m.ChatMessage, m.AgentRun | None]:
    """Record the user's turn and enqueue the run that answers it.

    `operator_role` is the acting token's `role` claim, recorded on the run so
    that code running LATER -- inside the run, with no request and no token --
    can still resolve what the human behind this chat could reach. See
    `control_tools._acting_token_role`. None for a channel sender (Telegram,
    Teams, ...): that door carries no token, and its authority is the
    binding row.

    `attachment_ids` are `FileAttachment` rows uploaded via
    `POST /chat/sessions/{id}/attachments` while still owned by this
    `ChatSession` (`owner_id == session.id`) -- re-pointed here to this turn's
    new `ChatMessage` (`owner_id == user_message.id`), matching the design
    doc's "temporarily the ChatSession.id ... then updated" Data Model note.
    From this call onward `api/v1/files.py`'s `_owned_attachment` resolves
    them through the message's session rather than the session directly.

    `context_refs` is what the operator attached with `#` for this one turn.
    Resolved here, not trusted: an id that names nothing this tenant can read
    is DROPPED rather than passed on, so a turn is never told to consult a
    knowledge base that does not exist. The surviving ids also NARROW this
    turn's retrieval -- see `retrieve_kb_context`'s `only_kb_ids`, which
    intersects them with the agent's own grants and can therefore only ever
    shrink what is searched.

    A leading slash command is parsed out of `message` here (`chat.modes`) and
    recorded on the turn. The mode is NOT a shortcut past the run: `/ask`
    still enqueues an ordinary `source="chat"` AgentRun on this session's one
    shared Task -- one that has no tools. Skipping the run would skip
    metering, the budget check, the audit trail and the department board, i.e.
    every reason this module funnels through `enqueue_run` at all.

    Returns `(user_message, None)` without enqueueing a run when the tenant's
    Assistant refuses the message outright (see the secret-blindness gate
    below) -- callers must not assume a run was always started.

    COMMITS `db` (via `enqueue_run`, or directly on the refusal path), which
    ends the transaction-local `app.tenant_id` RLS binding -- this function
    re-binds it before returning on both exits, same idiom as
    `collab.intake`'s handoff routing, so a caller reading `user_message`
    afterwards (e.g. `api/v1/chat.py`'s `_message_dto`, querying its
    attachments) does not have to know the trap exists.
    """
    history = await list_messages(db, tenant_id=tenant_id, session_id=session.id)
    # One parser for every door (web composer, Telegram free text, anything
    # later): the command is recognised here, not in a UI. `body` is what was
    # actually said -- an unknown command, a path, or a bare "/ask" comes back
    # unchanged with no mode, so a message that legitimately starts with a
    # slash is still sendable.
    if role == "followup":
        # A follow-up's text is ours, not a member's command.
        mode, body = None, message
    else:
        mode, body = parse_command(message)
    resolved_refs = await _resolve_context_refs(db, tenant_id=tenant_id, refs=context_refs)
    user_message = m.ChatMessage(
        tenant_id=tenant_id,
        session_id=session.id,
        role=role,
        content=body,
        mode=mode.key if mode is not None else None,
        context_refs=resolved_refs,
    )
    db.add(user_message)
    await db.flush()

    attachments: list[m.FileAttachment] = []
    if attachment_ids:
        # Scoped to THIS session's own pending uploads (`owner_id ==
        # session.id`), not just to the tenant: tenant + RLS alone would let
        # any id in the tenant be re-pointed onto this caller's message,
        # which is also the one thing that would then make it readable
        # through `files.py`'s `_owned_attachment` (it resolves a sent
        # attachment through its message's session). An id that names another
        # session's upload, or one already attached to a sent message, is
        # simply not found here -- no versioning, no re-attaching.
        attachments = list(
            (
                await db.execute(
                    select(m.FileAttachment).where(
                        m.FileAttachment.tenant_id == tenant_id,
                        m.FileAttachment.id.in_(attachment_ids),
                        m.FileAttachment.owner_type == "chat_message",
                        m.FileAttachment.owner_id == session.id,
                    )
                )
            ).scalars()
        )
        if attachments:
            await db.execute(
                update(m.FileAttachment)
                .where(m.FileAttachment.id.in_([a.id for a in attachments]))
                .values(owner_id=user_message.id)
            )

    # The tenant's unified Assistant is secret-blind by design (carried over
    # from the retired raw-completion Copilot path, oc8.copilot.redaction):
    # it never solicits, accepts, or forwards a credential value, checked
    # BEFORE the model ever sees the message -- not a system-prompt request
    # the model could ignore. Scoped to the Assistant only; an ordinary
    # agent's chat is unaffected -- this is a property of the Assistant's
    # structural-change surface, not a general chat restriction.
    agent = await db.get(m.Agent, session.agent_id)
    if agent is not None and agent.is_tenant_assistant and is_secret_request(message):
        # The already-flushed user turn carries the raw message -- redact it
        # in place before the durable commit below, same as the retired
        # copilot/chat.py redacted before every persistence boundary. Without
        # this, the credential the Assistant just refused would still land in
        # the transcript verbatim.
        user_message.content = redact_text(message)
        await db.flush()
        db.add(
            m.ChatMessage(
                tenant_id=tenant_id,
                session_id=session.id,
                role="assistant",
                content=_SECRET_REFUSAL,
            )
        )
        session.last_message_at = dt.datetime.now(tz=dt.UTC)
        await db.commit()
        # This commit ends the transaction-local `app.tenant_id` RLS binding --
        # re-bind here, not in every caller (`_message_dto` reads `user_message`
        # right after this returns). Same idiom as the enqueue_run exit below
        # and `collab.intake`'s handoff routing.
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        return user_message, None

    task_text = _build_task_text(history, body, new_role=role)
    if resolved_refs:
        # Quoted and attributed, never inlined as an instruction: the operator
        # is pointing at a source, and the agent already has `search_knowledge`
        # to read it with. Inlining a whole knowledge base here would blow the
        # prompt and defeat the retrieval narrowing set up below.
        named = ", ".join(f'"{ref["label"]}"' for ref in resolved_refs)
        if mode is not None and not mode.allows_tools:
            # /ask and /summarise offer no tool at all -- telling the model to
            # call one would only earn a refusal.
            task_text += f"\n\n[Context attached by the operator: knowledge base {named}.]"
        else:
            task_text += (
                f"\n\n[Context attached by the operator: knowledge base {named}. "
                "Search it with search_knowledge before answering; for this turn "
                "that tool reads nothing else.]"
            )
    for att in attachments:
        if att.is_image:
            continue
        label = f"\n\n[Attached file: {att.filename}]\n"
        label += (
            att.extracted_text if att.extracted_text else "(could not read this file's content)"
        )
        task_text += label

    context: dict[str, object] = {
        "task": task_text,
        "chat_session_id": str(session.id),
    }
    image_attachments = [att for att in attachments if att.is_image]
    if image_attachments:
        # Raw bytes are fetched at run-preamble time (Task 7), not here -- to
        # avoid holding large binary blobs in the run's persisted `context`
        # JSONB.
        context["task_images"] = [
            {"bucket_key": att.bucket_key, "content_type": att.content_type}
            for att in image_attachments
        ]
    if originating_operator is not None:
        context["originating_operator"] = originating_operator
    if operator_role is not None:
        context["operator_role"] = operator_role
    if chat_channel is not None and chat_channel_external_id is not None:
        context["chat_channel"] = chat_channel
        context["chat_channel_external_id"] = chat_channel_external_id
    # Which door this turn came through (design §7a.6). Read back by
    # `oc8.copilot.door` to decide whether `ask_user` may be offered.
    if role == "followup":
        context["door"] = "followup"
    elif chat_channel is not None and chat_channel_external_id is not None:
        context["door"] = "telegram"
    else:
        context["door"] = "web"
    if extra_context:
        # Never lets a caller overwrite who is acting or where the turn goes.
        reserved = {
            "originating_operator",
            "operator_role",
            "door",
            "chat_channel",
            "chat_channel_external_id",
            "chat_session_id",
            "task",
        }
        context.update({k: v for k, v in extra_context.items() if k not in reserved})
    if mode is not None:
        # Two consumers, deliberately: the model is TOLD (the directive below)
        # and the PEP ENFORCES (engine._authorize reads this key back through
        # mode_from_context). Neither is sufficient alone -- a told model can
        # ignore it, and a silently denied tool wastes a step.
        context["chat_mode"] = mode.key
        task_text += "\n\n" + mode_directive(mode)
        context["task"] = task_text
    if resolved_refs:
        context["context_kb_ids"] = [ref["id"] for ref in resolved_refs]

    # Open the session's Task HERE, on turn one, rather than letting the engine
    # open one implicitly when the run starts.
    #
    # `record_assistant_reply` used to be the only writer of `session.task_id`,
    # and it runs after a run reaches a terminal state -- so for the whole of
    # turn one the session had no task_id, and anything asking "who is the human
    # behind this task?" by looking the session up BY task_id found nothing.
    # `control_tools._member_may_reach_department` does exactly that and fails
    # closed, so the Assistant's very first cross-department `delegate_task` of
    # every new session was refused with "the person you are acting for does not
    # have access to that department" -- which was never true; the row simply
    # had not been backfilled yet.
    #
    # `enqueue_run`'s docstring already prescribes this for callers that own the
    # task ("must pass it here rather than setting it afterwards"): a worker can
    # claim the run in between and open a second task for the same work.
    if session.task_id is None and agent is not None:
        # Deferred: `oc8.agent.engine` reaches the runtime, which imports this
        # module. Resolved once, at first call -- same shape as the deferred
        # imports in `control_tools.py`.
        from oc8.agent.engine import open_run_task

        task = await open_run_task(db, agent=agent, task_text=task_text, tenant_id=tenant_id)
        session.task_id = task.id
        await db.flush()

    run, _published = await enqueue_run(
        db,
        tenant_id=tenant_id,
        agent_id=session.agent_id,
        context=context,
        source="chat",
        task_id=session.task_id,
    )
    # `enqueue_run` COMMITS, ending the transaction-local `app.tenant_id`
    # binding. Re-bind here rather than in the handler, so a caller reading
    # `user_message` afterwards (`_message_dto`, querying its attachments)
    # does not have to know the trap exists -- same idiom as
    # `collab.intake`'s handoff routing.
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )
    return user_message, run


async def record_assistant_reply(db: AsyncSession, *, run: m.AgentRun, output: str) -> None:
    """Called from `oc8.runtime.executor` once a `source="chat"` run reaches
    a terminal state -- turns its outcome into the durable transcript
    message the Chat UI reads. A no-op (logs nothing, raises nothing) for
    any run whose context carries no `chat_session_id`, so this is safe to
    call unconditionally from the executor's generic terminal-state path."""
    session_id_raw = run.context.get("chat_session_id") if run.context else None
    if not session_id_raw:
        return
    session = await get_session(
        db, tenant_id=run.tenant_id, session_id=uuid.UUID(str(session_id_raw))
    )
    if session is None:
        return
    # Redundant since `send_message` opens the task up front, and kept: a run
    # that reached here without one (a session row written before that change,
    # or a caller that enqueued a chat run some other way) still gets its
    # session linked rather than staying task-less for ever.
    if session.task_id is None and run.task_id is not None:
        session.task_id = run.task_id
    followup = (run.context or {}).get("followup")
    if isinstance(followup, dict):
        from oc8.copilot.followups import is_quiet_followup, load_responsibility_for_run

        resp = await load_responsibility_for_run(db, run=run)
        if is_quiet_followup(run, resp):
            # Nothing to report under notify_rule (§7a.3): the turn did its
            # work silently. last_update_at was already touched by the tool.
            session.last_message_at = session.last_message_at or dt.datetime.now(tz=dt.UTC)
            return
    rendered_components = (run.context or {}).get("rendered_components", [])
    db.add(
        m.ChatMessage(
            tenant_id=run.tenant_id,
            session_id=session.id,
            role="assistant",
            content=output or "",
            run_id=run.id,
            rendered_components=rendered_components,
        )
    )
    session.last_message_at = dt.datetime.now(tz=dt.UTC)
