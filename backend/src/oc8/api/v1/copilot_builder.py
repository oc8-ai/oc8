"""`/copilot/builder/*` -- the "Agent anlegen" card's routes (Copilot Agent
Builder §3.3/§3.4).

Writes are the chat owner's alone (Ruling 45): for anybody else -- a reader of
a shared chat included -- the proposal does not exist. A non-owner reads it
only once it was sent for approval, and only while they may view its target
department (Ruling A4); the mission is the owner's and the approver's alone."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental, require_permission
from oc8.authz.authority import authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, COPILOT_USE, DEPARTMENT, MANAGE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.copilot.agent_templates import (
    CORE_TEMPLATES,
    connected_kinds,
    merge_templates,
    pick_ideas,
)
from oc8.copilot.builder import (
    BUILDER_KIND,
    announce_builder,
    apply_builder_proposal,
    create_step,
    expire_if_due,
    load_steps,
    may_create_agent,
    request_builder_approval,
)
from oc8.copilot.builder_ops import BuilderInvalid, BuilderStep
from oc8.copilot.capabilities import (
    AgentModelSwitch,
    AgentNarrowingSet,
    AgentSkillAssign,
    GuardrailSet,
    MissionSet,
    TriggerCreate,
)
from oc8.copilot.models import CopilotProposal
from oc8.copilot.tools import uuid_or_none
from oc8.departments.repo import visible_departments
from oc8.schemas.dto import (
    AgentBuilderDTO,
    AgentIdeaDTO,
    BuilderAgentDTO,
    BuilderChoiceDTO,
    BuilderLimitDTO,
    BuilderTriggerDTO,
    LocalizedTextDTO,
)

router = APIRouter()

_Actor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]
_NOT_FOUND: Final = "agent proposal not found"
#: Refusals about the proposal's state, not its content: 409.
_CONFLICTS: Final = frozenset({"already_applied", "not_open", "expired", "superseded_concurrently"})
#: The states a proposal can only be in once it was sent for approval.
_SUBMITTED: Final = frozenset({"awaiting_approval", "applied", "rejected", "expired"})


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _refused(exc: BuilderInvalid) -> HTTPException:
    if exc.code == "not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    code = (
        status.HTTP_409_CONFLICT
        if exc.code in _CONFLICTS
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(code, f"agent_builder:{exc.code}")


async def _load(
    db: AsyncSession, actor: HumanActor, proposal_id: uuid.UUID
) -> tuple[CopilotProposal, bool, list[BuilderStep]]:
    """The proposal, whether the caller owns it, and its steps. Expired first,
    so whatever the caller reads next is already the current state."""
    proposal = await db.get(CopilotProposal, proposal_id)
    if (
        proposal is None
        or proposal.kind != BUILDER_KIND
        or proposal.tenant_id != actor.principal.tenant_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    if expire_if_due(proposal, now=_now()):
        await db.flush()
    session = await db.get(m.ChatSession, proposal.session_id) if proposal.session_id else None
    owner = (
        session is not None
        and session.member_id == actor.member.id
        and proposal.created_by == actor.member.subject
    )
    try:
        steps = await load_steps(db, proposal)
    except BuilderInvalid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from None
    return proposal, owner, steps


def _was_submitted(proposal: CopilotProposal) -> bool:
    return (
        isinstance((proposal.result or {}).get("approvalId"), str) and proposal.status in _SUBMITTED
    )


async def builder_dto(
    db: AsyncSession,
    proposal: CopilotProposal,
    steps: list[BuilderStep],
    *,
    may_create: bool,
    read_only: bool,
    show_mission: bool,
) -> AgentBuilderDTO:
    create = create_step(steps)
    department = await db.get(m.Department, create.departmentId)
    mission = create.mission
    model: BuilderChoiceDTO | None = None
    skills: list[BuilderChoiceDTO] = []
    triggers: list[BuilderTriggerDTO] = []
    systems: set[str] = set()
    limits: list[BuilderLimitDTO] = []
    for step in steps[1:]:
        op = step.operation
        if isinstance(op, MissionSet):
            mission = op.mission
        elif isinstance(op, AgentModelSwitch):
            mc = await db.get(m.ModelConfig, op.modelConfigId)
            model = BuilderChoiceDTO(
                id=str(op.modelConfigId), label=(mc.display_name or mc.model) if mc else ""
            )
        elif isinstance(op, AgentSkillAssign):
            version = await db.get(m.SkillVersion, op.skillVersionId)
            skill = await db.get(m.Skill, version.skill_id) if version is not None else None
            skills.append(
                BuilderChoiceDTO(id=str(op.skillVersionId), label=skill.name if skill else "")
            )
        elif isinstance(op, TriggerCreate):
            triggers.append(
                BuilderTriggerDTO(
                    kind=op.kind,
                    cron_expression=op.cronExpression,
                    event_source=op.eventSource,
                    event_type=op.eventType,
                    # The trigger's task is mission text too.
                    task_text=op.taskText if show_mission else "",
                )
            )
        elif isinstance(op, AgentNarrowingSet):
            tools = op.narrowing.get("tools", {})
            if isinstance(tools, dict):
                systems.update(k for k in tools if isinstance(k, str))
        elif isinstance(op, GuardrailSet):
            systems.add(op.connectionName)
            limits.append(
                BuilderLimitDTO(
                    connection_name=op.connectionName, function=op.function, decision=op.decision
                )
            )
    result = proposal.result or {}
    agent_dto: BuilderAgentDTO | None = None
    agent_id = uuid_or_none(result.get("agentId"))
    agent = await db.get(m.Agent, agent_id) if agent_id is not None else None
    if agent is not None and agent.tenant_id == proposal.tenant_id:
        agent_dto = BuilderAgentDTO(id=str(agent.id), name=agent.name, status=agent.status)
    refusal = result.get("refusal")
    failed_step = result.get("failedStep")
    rejected_reason = result.get("rejectedReason")
    is_draft = proposal.status == "draft"
    return AgentBuilderDTO(
        id=str(proposal.id),
        status=proposal.status,
        session_id=str(proposal.session_id) if proposal.session_id else None,
        department_id=str(create.departmentId),
        department_name=department.name if department is not None else "",
        name=create.name,
        role_title=create.roleTitle,
        mission=mission if show_mission else "",
        model=model,
        skills=skills,
        triggers=triggers,
        systems=sorted(systems),
        limits=limits,
        expires_at=proposal.expires_at.isoformat() if proposal.expires_at else None,
        may_create=may_create and is_draft and not read_only,
        may_request=is_draft and not read_only,
        read_only=read_only,
        refusal=refusal if isinstance(refusal, str) else None,
        failed_step=failed_step if isinstance(failed_step, int) else None,
        rejected_reason=rejected_reason if isinstance(rejected_reason, str) else None,
        agent=agent_dto,
    )


@router.get("/copilot/builder/{proposal_id}", response_model=AgentBuilderDTO)
async def get_builder(
    request: Request, proposal_id: uuid.UUID, db: DbSession, actor: _Actor
) -> AgentBuilderDTO:
    proposal, owner, steps = await _load(db, actor, proposal_id)
    department_id = create_step(steps).departmentId
    # A draft, a superseded one, one never sent: the member's alone. Once it is
    # in a queue, whoever may view that department reads it (Ruling A4).
    if not owner and not (_was_submitted(proposal) and actor.scope.may_view(department_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    authority = await authority_for_principal(request, db, actor.principal)
    may_create = may_create_agent(authority, actor.scope, department_id)
    # The approver is whoever the approvals funnel would let decide it.
    approver = actor.scope.may_decide(department_id) and may_create
    dto = await builder_dto(
        db,
        proposal,
        steps,
        may_create=owner and may_create,
        read_only=not owner,
        show_mission=owner or approver,
    )
    await db.commit()
    return dto


@router.post("/copilot/builder/{proposal_id}/apply", response_model=AgentBuilderDTO)
async def apply_builder(
    request: Request, proposal_id: uuid.UUID, db: DbSession, actor: _Actor
) -> AgentBuilderDTO:
    """ "Anlegen". The member must be allowed to create agents in the target
    department; otherwise the card offers "Zur Freigabe senden" instead."""
    proposal, owner, steps = await _load(db, actor, proposal_id)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    authority = await authority_for_principal(request, db, actor.principal)
    if not may_create_agent(authority, actor.scope, create_step(steps).departmentId):
        await db.commit()  # keep a lazy expiry
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"requires {perm(AGENT, MANAGE)} in this department"
        )
    try:
        await apply_builder_proposal(
            db,
            proposal_id=proposal.id,
            expect_status="draft",
            applied_by=actor.principal.subject,
            principal=actor.principal,
            now=_now(),
        )
    except BuilderInvalid as exc:
        # The refusal (and its step) stays on the proposal for the card.
        await db.commit()
        raise _refused(exc) from None
    await announce_builder(db, proposal)
    dto = await builder_dto(
        db, proposal, steps, may_create=True, read_only=False, show_mission=True
    )
    await db.commit()
    return dto


@router.post("/copilot/builder/{proposal_id}/request", response_model=AgentBuilderDTO)
async def request_builder(
    request: Request, proposal_id: uuid.UUID, db: DbSession, actor: _Actor
) -> AgentBuilderDTO:
    """ "Zur Freigabe senden": an approval in the target department's queue."""
    proposal, owner, steps = await _load(db, actor, proposal_id)
    if not owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    authority = await authority_for_principal(request, db, actor.principal)
    try:
        await request_builder_approval(
            db,
            proposal_id=proposal.id,
            requester=actor.member,
            scope=actor.scope,
            tenant_wide=tenant_wide_read(authority, perm(DEPARTMENT, VIEW)),
            now=_now(),
        )
    except BuilderInvalid as exc:
        await db.commit()
        raise _refused(exc) from None
    dto = await builder_dto(
        db,
        proposal,
        steps,
        may_create=may_create_agent(authority, actor.scope, create_step(steps).departmentId),
        read_only=False,
        show_mission=True,
    )
    await db.commit()
    return dto


@router.get(
    "/copilot/agent-ideas",
    response_model=list[AgentIdeaDTO],
    dependencies=[Depends(require_permission(COPILOT_USE))],
)
async def get_agent_ideas(request: Request, db: DbSession, actor: _Actor) -> list[AgentIdeaDTO]:
    templates = merge_templates(CORE_TEMPLATES, request.app.state.agent_templates)
    authority = await authority_for_principal(request, db, actor.principal)
    departments, _total = await visible_departments(
        db, scope=actor.scope, tenant_wide=tenant_wide_read(authority, perm(DEPARTMENT, VIEW))
    )
    # Only where a proposal may land -- builder_options_text and
    # create_builder_proposal apply the same seat check (A26): an idea must
    # never offer a system in a department the builder would refuse.
    proposable = [d for d in departments if actor.scope.may_view(d.id)]
    kinds = await connected_kinds(db, tenant_id=actor.principal.tenant_id, departments=proposable)
    return [
        AgentIdeaDTO(
            key=t.key,
            title=LocalizedTextDTO(en=t.title_en, de=t.title_de),
            subtitle=LocalizedTextDTO(en=t.subtitle_en, de=t.subtitle_de),
            prompt=LocalizedTextDTO(en=t.prompt_en, de=t.prompt_de),
        )
        for t in pick_ideas(templates, kinds)
    ]
