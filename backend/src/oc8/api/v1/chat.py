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
from oc8.agents.repo import visible_agent
from oc8.agents.versioning import pinned_version_no
from oc8.api.deps import CurrentPrincipal, DbSession, require_departmental, require_permission
from oc8.api.v1.run import run_to_dto
from oc8.authz.authority import Authority, authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, COPILOT, COPILOT_USE, MANAGE, RUN_START, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.chat.service import (
    create_session,
    delete_session,
    get_session,
    list_messages,
    list_sessions,
    rename_session,
    send_message,
)
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import ChatMessageDTO, ChatSessionDTO, FileAttachmentDTO, RunDTO
from oc8.schemas.requests import (
    CreateChatSessionRequest,
    RenameChatSessionRequest,
    SendChatMessageRequest,
)

router = APIRouter()


def _session_dto(session: m.ChatSession) -> ChatSessionDTO:
    return ChatSessionDTO(
        id=str(session.id),
        agent_id=str(session.agent_id),
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
    )


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
    return ChatMessageDTO(
        id=str(msg.id),
        session_id=str(msg.session_id),
        role=msg.role,
        content=msg.content,
        run_id=str(msg.run_id) if msg.run_id else None,
        rendered_components=msg.rendered_components,
        created_at=msg.created_at.isoformat(),
        attachments=attachments,
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
    )
    return [_session_dto(s) for s in sessions]


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
    is_assistant = await _assistant_visible(
        db, tenant_id=actor.principal.tenant_id, authority=authority, agent_id=body.agent_id
    )
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW)) or is_assistant
    agent = await visible_agent(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide,
        agent_id=body.agent_id,
        # `visible_agents`/`visible_agent` hide the Assistant from every screen
        # that browses agents, so this -- the one door that is deliberately
        # ABOUT the Assistant -- has to say so. `_assistant_visible` has already
        # established both halves: this caller holds copilot:use, and this id
        # is the tenant's Assistant and nothing else.
        include_tenant_assistant=is_assistant,
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
    return _session_dto(session)


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
    await _owned_session(request, session_id, db, actor)
    messages = await list_messages(db, tenant_id=actor.principal.tenant_id, session_id=session_id)
    return [await _message_dto(msg, db, actor.principal.tenant_id) for msg in messages]


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
    return _session_dto(session)


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
        originating_operator=actor.principal.subject,
        # The token's role claim, recorded on the run: a member with no
        # ASSIGNED role resolves to `permissions_for(token.role)` everywhere
        # else in this system, and the run that answers this message has no
        # token to read it from later. See `control_tools._acting_token_role`.
        operator_role=actor.principal.role,
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
