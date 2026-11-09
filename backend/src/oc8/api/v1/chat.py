"""Direct chat with one agent. See oc8.chat.service for the design: every
message is a real AgentRun (source="chat"), so guardrails/approvals apply
exactly as they do to an autonomous run."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agents.versioning import pinned_model_config_id, pinned_version_no, resolve_version
from oc8.api.deps import CurrentPrincipal, DbSession, require_departmental, require_permission
from oc8.api.v1._chat_common import (
    actor_viewer,
    session_detail,
    session_dto,
    session_owned_by_caller,
)
from oc8.api.v1.run import run_to_dto
from oc8.audit import append_event
from oc8.authz.authority import Authority, authority_for_principal
from oc8.authz.permissions import AGENT, COPILOT, COPILOT_USE, MANAGE, RUN_START, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.chat.audience import is_reader_only
from oc8.chat.model_choice import is_allowed_override, model_options
from oc8.chat.modes import MODES
from oc8.chat.questions import QuestionError
from oc8.chat.reach import member_can_reach_agent, reachable_chat_agent
from oc8.chat.service import (
    QuestionNotAnswerable,
    active_runs_for_session,
    answer_parked_question,
    create_session,
    delete_session,
    get_session,
    list_messages,
    list_sessions,
    regenerate_reply,
    rename_session,
    send_message,
)
from oc8.runtime.clarification import AnswerRefused
from oc8.runtime.intake import publish_run
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import (
    ActiveRunDTO,
    ChatContextRefDTO,
    ChatMessageDTO,
    ChatModeDTO,
    ChatModelOptionDTO,
    ChatSessionDTO,
    FileAttachmentDTO,
    RunDTO,
)
from oc8.schemas.requests import (
    ChatAnswerRequest,
    CreateChatSessionRequest,
    MessageFeedbackRequest,
    RenameChatSessionRequest,
    SendChatMessageRequest,
    SetChatModelRequest,
)

router = APIRouter()

#: Every chat route admits its actor the same way, then narrows by ownership.
_ChatActor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]


async def _message_dto(msg: m.ChatMessage, db: DbSession, tenant_id: uuid.UUID) -> ChatMessageDTO:
    result = await db.execute(
        select(m.FileAttachment).where(
            m.FileAttachment.tenant_id == tenant_id,
            m.FileAttachment.owner_type == "chat_message",
            m.FileAttachment.owner_id == msg.id,
        )
    )
    attachments = [
        FileAttachmentDTO(
            id=str(row.id),
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            is_image=row.is_image,
            created_at=row.created_at.isoformat(),
        )
        for row in result.scalars().all()
    ]
    author_name: str | None = None
    if msg.author_member_id is not None:
        author = await db.get(m.OrgMember, msg.author_member_id)
        author_name = (author.display_name or None) if author is not None else None
    return ChatMessageDTO(
        id=str(msg.id),
        session_id=str(msg.session_id),
        role=msg.role,
        content=msg.content,
        run_id=str(msg.run_id) if msg.run_id else None,
        rendered_components=msg.rendered_components,
        created_at=msg.created_at.isoformat(),
        attachments=attachments,
        mode=msg.mode,
        context_refs=[
            ChatContextRefDTO(
                kind=str(ref.get("kind", "")),
                id=str(ref.get("id", "")),
                label=str(ref.get("label", "")),
            )
            for ref in (msg.context_refs or [])
            if isinstance(ref, dict)
        ],
        author_member_id=str(msg.author_member_id) if msg.author_member_id else None,
        author_name=author_name,
        reply_to_id=str(msg.reply_to_id) if msg.reply_to_id else None,
        parts=msg.parts,
        suggestions=[
            str(o)
            for part in (msg.parts or [])
            if isinstance(part, dict) and part.get("type") == "suggestions"
            for o in (part.get("options") or [])
        ],
    )


class AssistantDTO(CamelModel):
    agent_id: str


@router.get(
    "/assistant",
    response_model=AssistantDTO,
    dependencies=[Depends(require_permission(COPILOT_USE))],
)
async def get_assistant(db: DbSession, principal: CurrentPrincipal) -> AssistantDTO:
    agent = await get_or_create_assistant(db, tenant_id=principal.tenant_id)
    await db.commit()
    return AssistantDTO(agent_id=str(agent.id))


@router.get(
    "/chat/modes",
    response_model=list[ChatModeDTO],
    dependencies=[Depends(require_permission(COPILOT_USE))],
)
async def get_chat_modes() -> list[ChatModeDTO]:
    """The slash commands the composer may offer, in picker order.

    Deliberately the same list `parse_command`/`mode_refusal` enforce against
    (`oc8.chat.modes.MODES`) -- so the picker can never advertise a command
    the backend would not honor, and vice versa.
    """
    return [
        ChatModeDTO(
            key=mode.key,
            summary=mode.summary,
            allows_tools=mode.allows_tools,
            allows_writes=mode.allows_writes,
        )
        for mode in MODES.values()
    ]


async def _assistant_visible(
    db: DbSession,
    *,
    tenant_id: uuid.UUID,
    authority: Authority,
    agent_id: uuid.UUID,
    permission: str = COPILOT_USE,
) -> bool:
    """Whether `agent_id` is the tenant's singleton Assistant AND the caller
    holds `permission` tenant-wide.

    Two callers, two different questions: `create_chat_session` asks "can this
    caller open THEIR OWN Assistant session at all" (default `COPILOT_USE`,
    held by every human role) while `_owned_session`'s foreign-session branch
    asks "can this caller read/reply in SOMEONE ELSE's Assistant session"
    (explicit `perm(COPILOT, MANAGE)`, held only by org_admin) -- the same
    function, two different bars, so a caller holding only `copilot:use`
    cannot walk through the second, stricter door.
    """
    if permission not in authority.tenant_wide:
        return False
    assistant = await get_or_create_assistant(db, tenant_id=tenant_id)
    return assistant.id == agent_id


@router.get("/chat/sessions", response_model=list[ChatSessionDTO])
async def get_sessions(
    db: DbSession,
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
    agent_id: Annotated[uuid.UUID | None, Query(alias="agentId")] = None,
) -> list[ChatSessionDTO]:
    sessions = await list_sessions(
        db,
        tenant_id=actor.principal.tenant_id,
        member_id=actor.member.id,
        agent_id=agent_id,
        pinned_first=True,
    )
    return [session_dto(s) for s in sessions]


@router.post("/chat/sessions", response_model=ChatSessionDTO, status_code=status.HTTP_201_CREATED)
async def create_chat_session(
    request: Request,
    body: CreateChatSessionRequest,
    db: DbSession,
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
) -> ChatSessionDTO:
    authority = await authority_for_principal(request, db, actor.principal)
    # The same predicate the realtime socket asks before it delivers this
    # agent's events (`oc8.chat.reach`): the Assistant with `copilot:use`,
    # every other agent through the scoped repository.
    agent = await reachable_chat_agent(
        db, authority=authority, scope=actor.scope, agent_id=body.agent_id
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    session = await create_session(
        db,
        tenant_id=actor.principal.tenant_id,
        agent_id=body.agent_id,
        member_id=actor.member.id,
    )
    await db.commit()
    return session_dto(session)


async def _owned_session(
    request: Request, session_id: uuid.UUID, db: DbSession, actor: HumanActor
) -> m.ChatSession:
    """Every chat-session sub-resource (messages, rename, delete) is gated by
    the same ownership check -- a session belongs to the member who created
    it, with the Assistant's tenant-wide `copilot:manage` carve-out on top."""
    session = await get_session(db, tenant_id=actor.principal.tenant_id, session_id=session_id)
    owned = session is not None and session.member_id == actor.member.id
    if session is not None and not owned:
        authority = await authority_for_principal(request, db, actor.principal)
        owned = await _assistant_visible(
            db,
            tenant_id=actor.principal.tenant_id,
            authority=authority,
            agent_id=session.agent_id,
            permission=perm(COPILOT, MANAGE),
        )
    if session is None or not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    return session


async def _readable_session(
    request: Request, session_id: uuid.UUID, db: DbSession, actor: HumanActor
) -> tuple[m.ChatSession, bool]:
    """`_owned_session`, or -- for the two READ routes only -- the session of
    which an edition made the caller a reader (and who still reaches its agent).
    The flag is `read_only`. Every write route keeps `_owned_session`; a reader
    never gets past it. Anything else is the same 404 as a session that does
    not exist."""
    try:
        return await _owned_session(request, session_id, db, actor), False
    except HTTPException:
        session = await get_session(db, tenant_id=actor.principal.tenant_id, session_id=session_id)
        if (
            session is not None
            and await is_reader_only(db, session=session, member_id=actor.member.id)
            and await member_can_reach_agent(
                db,
                member=actor.member,
                agent_id=session.agent_id,
                token_role=actor.principal.role,
            )
        ):
            return session, True
        raise


@router.get("/chat/sessions/{session_id}/messages", response_model=list[ChatMessageDTO])
async def get_messages(
    request: Request,
    session_id: uuid.UUID,
    db: DbSession,
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
) -> list[ChatMessageDTO]:
    await _readable_session(request, session_id, db, actor)
    messages = await list_messages(db, tenant_id=actor.principal.tenant_id, session_id=session_id)
    return [await _message_dto(msg, db, actor.principal.tenant_id) for msg in messages]


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDTO)
async def get_chat_session(
    request: Request, session_id: uuid.UUID, db: DbSession, actor: _ChatActor
) -> ChatSessionDTO:
    """One chat, with the runs still answering it -- so a page loaded mid-answer
    knows which run to watch. Same ownership gate as every session sub-route."""
    session, read_only = await _readable_session(request, session_id, db, actor)
    detail = await session_detail(db, viewer=await actor_viewer(db, actor), session=session)
    return detail.model_copy(update={"read_only": True}) if read_only else detail


@router.post("/chat/sessions/{session_id}/questions/{clarification_id}/answer")
async def answer_chat_question(
    request: Request,
    session_id: uuid.UUID,
    clarification_id: uuid.UUID,
    body: ChatAnswerRequest,
    db: DbSession,
    actor: _ChatActor,
) -> dict[str, str]:
    """The chat's own door to a question its run parked on (spec §4.1).

    Only the person the run acts for answers here: the session's owner, and --
    when the turn carried one -- the run's originating operator. Not the
    Assistant's `copilot:manage` carve-out that lets an admin READ a colleague's
    session: answering puts words in that colleague's mouth. No department seat
    is needed, unlike the inbox's /clarifications door, because the run is
    acting for this person. 404 for anything that is not theirs, 409 once
    answered, 422 for an answer that does not fit the questions.
    """
    session = await _owned_session(request, session_id, db, actor)
    clarification = (
        await db.execute(
            select(m.Clarification)
            .where(m.Clarification.id == clarification_id)
            .execution_options(populate_existing=True)
            # Two tabs answering at once serialise here; the second sees the
            # question answered (same lock as the inbox door).
            .with_for_update()
        )
    ).scalar_one_or_none()
    run = (
        (
            await db.execute(
                select(m.AgentRun)
                .where(m.AgentRun.id == clarification.run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if clarification is not None
        else None
    )
    context = (run.context or {}) if run is not None else {}
    operator = context.get("originating_operator")
    if (
        clarification is None
        or run is None
        or run.source != "chat"
        or context.get("chat_session_id") != str(session.id)
        or session.member_id != actor.member.id
        or (operator is not None and operator != actor.principal.subject)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "question not found")
    # An empty body is refused by `answer_parked_question` AFTER it has checked
    # the question is still open -- an answered question is a 409 whatever the
    # body says.
    try:
        await answer_parked_question(
            db,
            session=session,
            run=run,
            clarification=clarification,
            text=body.text or "",
            answers=body.answers,
            member=actor.member,
            via="chat",
            principal=actor.principal,
        )
    except AnswerRefused as exc:
        # The secret gate every answer door shares (`resolve_clarification`):
        # nothing was written, the run stays parked.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except QuestionNotAnswerable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except QuestionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    run_id, tenant_id = run.id, run.tenant_id
    await db.commit()
    # Strictly after the commit: a stream entry whose run row is not yet
    # visible is a run a worker picks up and cannot find.
    await publish_run(run_id=run_id, tenant_id=tenant_id)
    return {"runId": str(run_id)}


async def _session_agent(
    request: Request, db: DbSession, actor: HumanActor, session: m.ChatSession
) -> m.Agent:
    """The session's agent, through the same scoped predicate as every other
    chat door (`oc8.chat.reach`): a caller who lost the agent gets a 404."""
    authority = await authority_for_principal(request, db, actor.principal)
    agent = await reachable_chat_agent(
        db, authority=authority, scope=actor.scope, agent_id=session.agent_id
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return agent


@router.get("/chat/sessions/{session_id}/models", response_model=list[ChatModelOptionDTO])
async def get_chat_models(
    request: Request, session_id: uuid.UUID, db: DbSession, actor: _ChatActor
) -> list[ChatModelOptionDTO]:
    """The model picker: "Auto" first, then the agent's own model, then the
    tenant's other chat-enabled models (`oc8.chat.model_choice`)."""
    session = await _owned_session(request, session_id, db, actor)
    agent = await _session_agent(request, db, actor, session)
    pinned = await resolve_version(db, None, agent)
    options = await model_options(db, agent=agent, default_model_id=pinned_model_config_id(pinned))
    return [
        ChatModelOptionDTO(
            id=o.id,
            label=o.label,
            provider=o.provider,
            locality=o.locality,
            is_default=o.is_default,
        )
        for o in options
    ]


@router.put("/chat/sessions/{session_id}/model", response_model=ChatSessionDTO)
async def set_chat_model(
    request: Request,
    session_id: uuid.UUID,
    body: SetChatModelRequest,
    db: DbSession,
    actor: _ChatActor,
) -> ChatSessionDTO:
    """Pin this chat to one model, or back to "Auto" with `null`. Only a model
    the picker offers is accepted; the run re-checks it when it resolves."""
    session = await _owned_session(request, session_id, db, actor)
    if body.model_config_id is not None:
        agent = await _session_agent(request, db, actor, session)
        if not await is_allowed_override(db, agent=agent, model_config_id=body.model_config_id):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "model not allowed for this agent"
            )
    session.model_override_id = body.model_config_id
    await db.flush()
    dto = session_dto(session)
    await db.commit()
    return dto


@router.patch("/chat/sessions/{session_id}", response_model=ChatSessionDTO)
async def rename_chat_session(
    request: Request,
    session_id: uuid.UUID,
    body: RenameChatSessionRequest,
    db: DbSession,
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
) -> ChatSessionDTO:
    session = await _owned_session(request, session_id, db, actor)
    await rename_session(db, session=session, title=body.title)
    await db.commit()
    return session_dto(session)


@router.delete("/chat/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_session(
    request: Request,
    session_id: uuid.UUID,
    db: DbSession,
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
) -> None:
    session = await _owned_session(request, session_id, db, actor)
    await delete_session(db, tenant_id=actor.principal.tenant_id, session=session)
    await db.commit()


@router.post(
    "/chat/sessions/{session_id}/messages",
    response_model=ChatMessageDTO,
    status_code=status.HTTP_201_CREATED,
)
async def post_message(
    request: Request,
    session_id: uuid.UUID,
    body: SendChatMessageRequest,
    db: DbSession,
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
) -> ChatMessageDTO:
    session = await _owned_session(request, session_id, db, actor)
    authority = await authority_for_principal(request, db, actor.principal)
    is_assistant_session = await _assistant_visible(
        db, tenant_id=actor.principal.tenant_id, authority=authority, agent_id=session.agent_id
    )
    # RUN_START is a tenant-wide permission a bare copilot:use member does not
    # hold, and rightly so for every OTHER agent's chat -- starting a run
    # against a departmental agent is real work, gated the same as any other
    # RUN_START-gated door. The Assistant is the one exception: `copilot:use`
    # alone is enough to talk to it, which is the entire point of this slice.
    if not is_assistant_session and RUN_START not in authority.tenant_wide:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"requires permission: {RUN_START}"
        )
    user_message, run = await send_message(
        db,
        session=session,
        tenant_id=actor.principal.tenant_id,
        message=body.message,
        attachment_ids=body.attachment_ids,
        context_refs=[{"kind": ref.kind, "id": ref.id} for ref in body.context_refs],
        originating_operator=actor.principal.subject,
        # The token's role claim, recorded on the run: a member with no
        # ASSIGNED role resolves to `permissions_for(token.role)` everywhere
        # else in this system, and the run that answers this message has no
        # token to read it from later. See `control_tools._acting_token_role`.
        operator_role=actor.principal.role,
        author_member_id=actor.member.id,
        # The member behind THIS request's token, whose role is the one above:
        # the turn's pinned context is resolved under their rights.
        acting_member=actor.member,
    )
    dto = await _message_dto(user_message, db, actor.principal.tenant_id)
    if run is not None:
        # NOT persisted -- user_message.run_id stays NULL in the database,
        # same as every other user turn (a later GET of this same message
        # still reports runId: null). This is a same-response-only signal:
        # the caller now learns which run was just enqueued to answer THIS
        # specific message, without a second round trip. Frontend use:
        # DraftWithCopilot (agent-instructions-panel.tsx) and CopilotDock
        # (copilot-dock.tsx) both talk to the single tenant-wide Assistant
        # session, so two concurrent requests can interleave in the same
        # transcript -- matching "my reply" by transcript position/length
        # is unreliable there, but the assistant's eventual ChatMessage DOES
        # persist run_id (record_assistant_reply), so a caller that knows
        # its own run id up front can wait for the specific message whose
        # runId matches, regardless of what else lands in between.
        dto = dto.model_copy(update={"run_id": str(run.id)})
    return dto


async def _session_message(
    db: DbSession, *, session: m.ChatSession, message_id: uuid.UUID
) -> m.ChatMessage:
    msg = await db.get(m.ChatMessage, message_id)
    if msg is None or msg.session_id != session.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    return msg


@router.post(
    "/chat/sessions/{session_id}/messages/{message_id}/regenerate",
    response_model=ActiveRunDTO,
    status_code=status.HTTP_201_CREATED,
)
async def regenerate_message(
    request: Request,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    db: DbSession,
    actor: _ChatActor,
) -> ActiveRunDTO:
    """Answer a user turn again; both answers stay (spec §2.1)."""
    session = await session_owned_by_caller(session_id, db, actor)
    msg = await _session_message(db, session=session, message_id=message_id)
    if msg.role != "user":
        raise HTTPException(status.HTTP_409_CONFLICT, "only a member's turn can be answered again")
    # Serialise concurrent regenerates: the lock is held until `enqueue_run`
    # commits, so a second click waits and then sees the first one's run.
    await db.execute(
        select(m.ChatSession.id).where(m.ChatSession.id == session.id).with_for_update()
    )
    running = await active_runs_for_session(
        db, tenant_id=actor.principal.tenant_id, session_id=session.id
    )
    if any((r.context or {}).get("reply_to_message_id") == str(msg.id) for r in running):
        raise HTTPException(status.HTTP_409_CONFLICT, "an answer to this turn is still running")
    authority = await authority_for_principal(request, db, actor.principal)
    is_assistant = await _assistant_visible(
        db, tenant_id=actor.principal.tenant_id, authority=authority, agent_id=session.agent_id
    )
    if not is_assistant and RUN_START not in authority.tenant_wide:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires permission: {RUN_START}")
    run = await regenerate_reply(
        db,
        session=session,
        tenant_id=actor.principal.tenant_id,
        user_message=msg,
        originating_operator=actor.principal.subject,
        operator_role=actor.principal.role,
        acting_member=actor.member,
    )
    return ActiveRunDTO(run_id=str(run.id), reply_to_id=str(msg.id), state=run.state)


@router.post(
    "/chat/sessions/{session_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def message_feedback(
    request: Request,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    body: MessageFeedbackRequest,
    db: DbSession,
    actor: _ChatActor,
) -> None:
    session = await session_owned_by_caller(session_id, db, actor)
    msg = await _session_message(db, session=session, message_id=message_id)
    if msg.role != "assistant":
        raise HTTPException(status.HTTP_409_CONFLICT, "only an answer can be rated")
    await append_event(
        db,
        tenant_id=actor.principal.tenant_id,
        actor_type="operator",
        actor_id=actor.member.id,
        category="chat",
        action="chat.feedback",
        resource={
            "session_id": str(session.id),
            "message_id": str(msg.id),
            "run_id": str(msg.run_id) if msg.run_id else None,
            "rating": body.rating,
            "note": body.note,
        },
        principal=actor.principal,
    )
    await db.commit()


@router.get("/chat/sessions/{session_id}/runs/{run_id}", response_model=RunDTO)
async def get_session_run(
    request: Request,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    db: DbSession,
    actor: Annotated[
        HumanActor,
        Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
    ],
) -> RunDTO:
    """One run's live state, for the frontend's `useCopilotRunActivity` poll.

    Ownership-scoped through `_owned_session`, exactly as `get_messages` is:
    the caller must own the chat session (or hold `copilot:manage` and be
    reading a colleague's Assistant session, per that function's oversight
    carve-out). The run itself must additionally belong to THIS session --
    checked via the run's own `context["chat_session_id"]`, written when the
    run is enqueued (`chat/service.send_message`) -- so a caller who owns
    some OTHER session cannot read a run by guessing its id.
    """
    await _owned_session(request, session_id, db, actor)
    run_row = await db.get(m.AgentRun, run_id)
    if run_row is None or (run_row.context or {}).get("chat_session_id") != str(session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    # Same `pinned_version_no` helper `GET /runs/{id}` (run.py) uses, so
    # `agentVersionNo` is filled identically on both routes that serialize a
    # `RunDTO` for the same frontend query-cache entry (`["run", runId]`).
    return run_to_dto(run_row, agent_version_no=await pinned_version_no(db, run_row))
