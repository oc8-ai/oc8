"""The chat's own door to the Copilot's direct access (copilot direct access
§3, §5, §8): the conversation's member sees, decides and revokes -- and only
they do. No seat is needed: the access is theirs, for their own conversation.
Anything that is not theirs is the same 404 as something that does not exist."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1._serializers import approval_to_dto, resolve_approval_names
from oc8.approvals import (
    AlreadyDecided,
    EffectRefused,
    NotYourDepartment,
    NotYourSayAtAll,
    UnknownDecision,
    awaiting_department,
    decide_approval,
    department_after_person,
    load_direct_access_card,
    member_direct_access_cards,
)
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.chat.service import get_session
from oc8.connections.direct_access import active_grants, revoke_grant
from oc8.connections.usable import tool_key_label
from oc8.runtime.intake import publish_run
from oc8.schemas.dto import ApprovalDTO, DirectAccessGrantDTO
from oc8.schemas.requests import ApprovalDecisionRequest

router = APIRouter()

_ChatActor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]


async def _own_session(db: DbSession, actor: HumanActor, session_id: uuid.UUID) -> m.ChatSession:
    session = await get_session(db, tenant_id=actor.principal.tenant_id, session_id=session_id)
    if session is None or session.member_id != actor.member.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    return session


@router.get("/chat/direct-access/approvals", response_model=list[ApprovalDTO])
async def my_direct_access_approvals(db: DbSession, actor: _ChatActor) -> list[ApprovalDTO]:
    rows = await member_direct_access_cards(db, member_id=actor.member.id)
    grants = {
        g.approval_id: g
        for g in (
            await db.execute(
                select(m.DirectAccessGrant).where(
                    m.DirectAccessGrant.approval_id.in_([r.id for r in rows]),
                    m.DirectAccessGrant.revoked_at.is_(None),
                )
            )
        ).scalars()
    }
    names = await resolve_approval_names(db, rows)
    waiting = await awaiting_department(db, [r.id for r in rows])
    out: list[ApprovalDTO] = []
    for row in rows:
        dto = approval_to_dto(row, names)
        if dto.direct_access is not None and row.id in grants:
            dto.direct_access.grant_id = str(grants[row.id].id)
            dto.direct_access.grant_active = True
        if dto.direct_access is not None and row.id in waiting:
            dto.direct_access.awaiting_department = True
        out.append(dto)
    return out


@router.post("/chat/direct-access/approvals/{approval_id}/decision", response_model=ApprovalDTO)
async def decide_direct_access(
    approval_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    db: DbSession,
    actor: _ChatActor,
) -> ApprovalDTO:
    approval = await load_direct_access_card(db, approval_id, member_id=actor.member.id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    try:
        result = await decide_approval(
            db,
            approval,
            decision=body.decision,
            tenant_id=actor.principal.tenant_id,
            actor=actor,
            reason=body.reason or None,
        )
    except NotYourDepartment as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found") from exc
    except NotYourSayAtAll as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except UnknownDecision as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown decision") from exc
    except AlreadyDecided as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except EffectRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    dto = approval_to_dto(result.approval)
    dto.resumed = result.resumed_run_id is not None
    if (
        dto.direct_access is not None
        and result.approval.status == "approved"
        and department_after_person(result.approval) is not None
    ):
        # Approved by the person: the department is asked now.
        dto.direct_access.awaiting_department = True
    await db.commit()
    if result.resumed_run_id is not None:
        # After the commit: a stream entry whose run is not yet visible is a
        # run a worker picks up and cannot find.
        await publish_run(run_id=result.resumed_run_id, tenant_id=actor.principal.tenant_id)
    return dto


@router.get("/chat/sessions/{session_id}/direct-access", response_model=list[DirectAccessGrantDTO])
async def session_direct_access(
    session_id: uuid.UUID, db: DbSession, actor: _ChatActor
) -> list[DirectAccessGrantDTO]:
    session = await _own_session(db, actor, session_id)
    return [
        DirectAccessGrantDTO(
            id=str(g.id),
            session_id=str(g.chat_session_id),
            tool_key=g.tool_key,
            label=await tool_key_label(db, g.tool_key),
            approval_id=str(g.approval_id),
            granted_at=g.granted_at.isoformat(),
        )
        for g in await active_grants(db, session_id=session.id)
    ]


@router.post(
    "/chat/sessions/{session_id}/direct-access/{grant_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_direct_access(
    session_id: uuid.UUID, grant_id: uuid.UUID, db: DbSession, actor: _ChatActor
) -> None:
    session = await _own_session(db, actor, session_id)
    grant = await db.get(m.DirectAccessGrant, grant_id)
    if grant is None or grant.chat_session_id != session.id or grant.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "grant not found")
    await revoke_grant(db, grant=grant, member_id=actor.member.id)
    await db.commit()
