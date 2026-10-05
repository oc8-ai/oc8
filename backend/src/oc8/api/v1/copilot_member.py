"""`/copilot/*` -- the calling member's own Copilot layer (design §7a, §9).

There is deliberately no way to address another member: every route reads
`actor.member.id`, and a foreign id is 404, never 403. (The human-reviewed
proposal API lives next door in `copilot.py`.)"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, text

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agents.versioning import pinned_version_no
from oc8.api.deps import DbSession, require_departmental, require_permission
from oc8.api.v1.run import run_to_dto
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.copilot import followups as fu
from oc8.copilot import notes as nt
from oc8.copilot import profile as pr
from oc8.copilot import responsibilities as rs
from oc8.realtime.emit import publish_copilot_changed
from oc8.schemas.dto import (
    CopilotNoteDTO,
    CopilotProfileDTO,
    FollowupDTO,
    ResponsibilityDTO,
    RunDTO,
)
from oc8.schemas.requests import UpdateCopilotProfileRequest, UpdateResponsibilityRequest

router = APIRouter(dependencies=[Depends(require_permission(COPILOT_USE))])
Actor = Annotated[
    HumanActor,
    Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
]


async def _profile_dto(db: DbSession, actor: HumanActor) -> CopilotProfileDTO:
    tenant = actor.principal.tenant_id
    assistant = await get_or_create_assistant(db, tenant_id=tenant)
    p = await pr.get_or_create_profile(db, tenant_id=tenant, member_id=actor.member.id)
    status_, count = await pr.computed_status(
        db, tenant_id=tenant, member_id=actor.member.id, assistant_id=assistant.id
    )
    return CopilotProfileDTO(
        display_name=p.display_name,
        avatar=p.avatar,
        paused_at=p.paused_at,
        status=status_,
        active_count=count,
        agent_id=str(assistant.id),
    )


def _responsibility_dto(r: m.Responsibility) -> ResponsibilityDTO:
    return ResponsibilityDTO(
        id=str(r.id),
        title=r.title,
        goal=r.goal,
        state=r.state,
        next_step=r.next_step,
        notify_rule=r.notify_rule,
        origin_channel=r.origin_channel,
        last_update_at=r.last_update_at,
        created_at=r.created_at,
        chat_session_id=str(r.chat_session_id),
    )


@router.get("/copilot/profile", response_model=CopilotProfileDTO)
async def get_profile(db: DbSession, actor: Actor) -> CopilotProfileDTO:
    dto = await _profile_dto(db, actor)
    await db.commit()
    return dto


@router.patch("/copilot/profile", response_model=CopilotProfileDTO)
async def patch_profile(
    body: UpdateCopilotProfileRequest, db: DbSession, actor: Actor
) -> CopilotProfileDTO:
    tenant = actor.principal.tenant_id
    try:
        await pr.update_profile(
            db,
            tenant_id=tenant,
            member_id=actor.member.id,
            display_name=body.display_name,
            avatar=body.avatar,
        )
    except pr.ProfileError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    dto = await _profile_dto(db, actor)
    await db.commit()
    await publish_copilot_changed(tenant, member_id=actor.member.id, kind="profile")
    return dto


@router.post("/copilot/pause", response_model=CopilotProfileDTO)
async def post_pause(db: DbSession, actor: Actor) -> CopilotProfileDTO:
    tenant = actor.principal.tenant_id
    await pr.pause(db, tenant_id=tenant, member_id=actor.member.id)
    dto = await _profile_dto(db, actor)
    await db.commit()
    await publish_copilot_changed(tenant, member_id=actor.member.id, kind="profile")
    return dto


@router.post("/copilot/resume", response_model=CopilotProfileDTO)
async def post_resume(db: DbSession, actor: Actor) -> CopilotProfileDTO:
    tenant = actor.principal.tenant_id
    await pr.resume(db, tenant_id=tenant, member_id=actor.member.id)
    await db.commit()
    # The commit ends the transaction-local tenant binding (chat/service.py
    # idiom); catch_up re-binds after each of its own commits.
    await db.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant)})
    await fu.catch_up(db, tenant_id=tenant, member_id=actor.member.id)
    dto = await _profile_dto(db, actor)
    await db.commit()
    await publish_copilot_changed(tenant, member_id=actor.member.id, kind="profile")
    return dto


@router.get("/copilot/responsibilities", response_model=list[ResponsibilityDTO])
async def list_responsibilities(
    db: DbSession, actor: Actor, state: str | None = None
) -> list[ResponsibilityDTO]:
    states = [s.strip() for s in state.split(",") if s.strip()] if state else None
    rows = await rs.list_responsibilities(
        db, tenant_id=actor.principal.tenant_id, member_id=actor.member.id, states=states
    )
    return [_responsibility_dto(r) for r in rows]


@router.patch("/copilot/responsibilities/{responsibility_id}", response_model=ResponsibilityDTO)
async def patch_responsibility(
    responsibility_id: uuid.UUID,
    body: UpdateResponsibilityRequest,
    db: DbSession,
    actor: Actor,
) -> ResponsibilityDTO:
    tenant = actor.principal.tenant_id
    try:
        if body.state == "done" or body.state == "cancelled":
            r = await rs.close_responsibility(
                db,
                tenant_id=tenant,
                member_id=actor.member.id,
                responsibility_id=responsibility_id,
                state=body.state,
                reason=body.reason,
                actor_agent_id=None,
                member_subject=actor.member.subject,
            )
        else:
            r = await rs.update_responsibility(
                db,
                tenant_id=tenant,
                member_id=actor.member.id,
                responsibility_id=responsibility_id,
                state=body.state,
            )
    except rs.ResponsibilityError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in str(exc)
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(code, str(exc)) from exc
    dto = _responsibility_dto(r)
    await db.commit()
    await publish_copilot_changed(tenant, member_id=actor.member.id, kind="responsibility")
    return dto


@router.get("/copilot/followups", response_model=list[FollowupDTO])
async def list_followups(db: DbSession, actor: Actor) -> list[FollowupDTO]:
    tenant = actor.principal.tenant_id
    triggers = await fu.list_followups(db, tenant_id=tenant, member_id=actor.member.id)
    titles = {
        r.id: r.title
        for r in await rs.list_responsibilities(db, tenant_id=tenant, member_id=actor.member.id)
    }
    return [
        FollowupDTO(
            id=str(t.id),
            responsibility_id=str(t.responsibility_id),
            responsibility_title=titles.get(t.responsibility_id, "") if t.responsibility_id else "",
            kind=t.kind,
            cron_expression=t.cron_expression,
            timezone=t.timezone,
            next_run_at=t.next_run_at,
            ends_at=t.ends_at,
            enabled=t.enabled,
            last_skip_reason=t.last_skip_reason,
            purpose=t.followup_purpose or "check_in",
        )
        for t in triggers
    ]


@router.delete("/copilot/followups/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_followup(trigger_id: uuid.UUID, db: DbSession, actor: Actor) -> Response:
    tenant = actor.principal.tenant_id
    if not await fu.cancel_followup(
        db, tenant_id=tenant, member_id=actor.member.id, trigger_id=trigger_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "follow-up not found")
    await db.commit()
    await publish_copilot_changed(tenant, member_id=actor.member.id, kind="followup")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/copilot/delegations", response_model=list[RunDTO])
async def list_delegations(
    db: DbSession, actor: Actor, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[RunDTO]:
    """Runs of other agents that started from one of the caller's Copilot chats."""
    tenant = actor.principal.tenant_id
    assistant = await get_or_create_assistant(db, tenant_id=tenant)
    sessions = list(
        (
            await db.execute(
                select(m.ChatSession.id).where(
                    m.ChatSession.tenant_id == tenant,
                    m.ChatSession.member_id == actor.member.id,
                    m.ChatSession.agent_id == assistant.id,
                )
            )
        ).scalars()
    )
    if not sessions:
        return []
    rows = (
        await db.execute(
            select(m.AgentRun)
            .where(
                m.AgentRun.tenant_id == tenant,
                m.AgentRun.agent_id != assistant.id,
                m.AgentRun.context["chat_session_id"].astext.in_([str(s) for s in sessions]),
            )
            .order_by(m.AgentRun.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [run_to_dto(run, agent_version_no=await pinned_version_no(db, run)) for run in rows]


@router.get("/copilot/notes", response_model=list[CopilotNoteDTO])
async def list_notes(db: DbSession, actor: Actor) -> list[CopilotNoteDTO]:
    tenant = actor.principal.tenant_id
    assistant = await get_or_create_assistant(db, tenant_id=tenant)
    rows = await nt.list_notes(
        db, tenant_id=tenant, member_id=actor.member.id, assistant_id=assistant.id
    )
    titles = {
        str(r.id): r.title
        for r in await rs.list_responsibilities(db, tenant_id=tenant, member_id=actor.member.id)
    }

    def _dto(n: m.MemoryRecord) -> CopilotNoteDTO:
        rid = (n.record_metadata or {}).get("responsibility_id")
        return CopilotNoteDTO(
            id=str(n.id),
            content=n.content,
            created_at=n.created_at,
            responsibility_id=str(rid) if rid else None,
            # Only the member's OWN responsibilities resolve to a title.
            responsibility_title=titles.get(str(rid)) if rid else None,
        )

    return [_dto(n) for n in rows]


@router.delete("/copilot/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: uuid.UUID, db: DbSession, actor: Actor) -> Response:
    tenant = actor.principal.tenant_id
    assistant = await get_or_create_assistant(db, tenant_id=tenant)
    if not await nt.delete_note(
        db,
        tenant_id=tenant,
        member_id=actor.member.id,
        assistant_id=assistant.id,
        note_id=note_id,
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
