"""What the chat routers share: how a chat is serialized, who the caller is to
the context registry, and the owner-only session gate."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.v1._serializers import question_dtos
from oc8.authz.scope import HumanActor
from oc8.chat.context.base import Viewer, viewer_for_member
from oc8.chat.projects import pinned_refs_for_viewer
from oc8.chat.service import active_runs_for_session, get_session, parked_session_question
from oc8.schemas.dto import ActiveRunDTO, ChatSessionDTO, ContextRefViewDTO, PendingQuestionDTO


def session_dto(
    session: m.ChatSession,
    *,
    active_runs: Sequence[m.AgentRun] = (),
    read_only: bool = False,
    pending_question: PendingQuestionDTO | None = None,
) -> ChatSessionDTO:
    return ChatSessionDTO(
        id=str(session.id),
        agent_id=str(session.agent_id),
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        model_override_id=str(session.model_override_id) if session.model_override_id else None,
        kind=getattr(session, "kind", "chat") or "chat",
        active_runs=[
            ActiveRunDTO(
                run_id=str(run.id),
                reply_to_id=(run.context or {}).get("reply_to_message_id"),
                state=run.state,
            )
            for run in active_runs
        ],
        read_only=read_only,
        pending_question=pending_question,
        task_id=str(session.task_id) if session.task_id else None,
        project_id=str(session.project_id) if session.project_id else None,
        pinned=bool(session.pinned),
        mode=session.mode,
        effort=session.effort,
    )


async def pending_question(
    db: AsyncSession, runs: Sequence[m.AgentRun]
) -> PendingQuestionDTO | None:
    """What the session's active run is parked on, for the question card."""
    parked = await parked_session_question(db, runs=runs)
    if parked is None:
        return None
    run, clarification = parked
    return PendingQuestionDTO(
        clarification_id=str(clarification.id),
        run_id=str(run.id),
        text=clarification.question,
        questions=question_dtos((run.context or {}).get("pending_questions")),
    )


async def actor_viewer(db: AsyncSession, actor: HumanActor) -> Viewer:
    """The caller, with the role on THIS request's token (never a stored one)."""
    return await viewer_for_member(db, actor.member, token_role=actor.principal.role)


async def ref_views(
    db: AsyncSession, viewer: Viewer, refs: Sequence[Mapping[str, Any]]
) -> list[ContextRefViewDTO]:
    """Stored pins as `viewer` sees them now."""
    views = await pinned_refs_for_viewer(
        db, viewer=viewer, refs=[r for r in refs if isinstance(r, Mapping)]
    )
    return [
        ContextRefViewDTO(kind=v.kind, id=v.id, label=v.label, available=v.available) for v in views
    ]


async def session_detail(
    db: AsyncSession, *, viewer: Viewer, session: m.ChatSession, read_only: bool = False
) -> ChatSessionDTO:
    """One chat in full: the runs still answering it (so a page knows which
    run to watch), the question one is parked on, and its pins re-checked for
    `viewer`. Every route that answers with a single chat answers with this,
    so a cache write from any of them keeps the run watcher."""
    runs = await active_runs_for_session(db, tenant_id=session.tenant_id, session_id=session.id)
    # A reader (read-only share) sees no parked question and never the label the
    # owner stored for a pin the reader cannot open.
    dto = session_dto(
        session,
        active_runs=runs,
        read_only=read_only,
        pending_question=None if read_only else await pending_question(db, runs),
    )
    pins = await ref_views(db, viewer, list(session.pinned_refs or []))
    if read_only:
        pins = [p if p.available else p.model_copy(update={"label": ""}) for p in pins]
    return dto.model_copy(update={"pinned_refs": pins})


async def session_owned_by_caller(
    session_id: uuid.UUID, db: AsyncSession, actor: HumanActor
) -> m.ChatSession:
    """Owner-only: no `copilot:manage` oversight carve-out. Acting on a turn
    (regenerating it, rating it) or rearranging a chat (its pins, project,
    pinned state) speaks for the member's own conversation, which an overseer
    reading it must not be able to do."""
    session = await get_session(db, tenant_id=actor.principal.tenant_id, session_id=session_id)
    if session is None or session.member_id != actor.member.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    return session
