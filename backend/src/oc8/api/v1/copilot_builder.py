"""`/copilot/builder/*` -- the "Agent anlegen" card's routes (Copilot Agent
Builder §3.3/§3.4).

Writes are the chat owner's alone (Ruling 45): for anybody else -- a reader of
a shared chat included -- the proposal does not exist. A non-owner reads it
only once it was sent for approval, and only while they may view its target
department (Ruling A4); the mission is the owner's and the approver's alone."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental, require_permission
from oc8.authz.authority import Authority, authority_for_principal, tenant_wide_read
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
    SeesBoard,
    announce_builder,
    apply_builder_proposal,
    board_spec,
    board_step,
    board_title,
    create_step,
    expire_if_due,
    load_steps,
    may_apply,
    new_department,
    request_builder_approval,
    target_department,
)
from oc8.copilot.builder_notice import tell_copilot_best_effort
from oc8.copilot.builder_ops import BuilderInvalid, BuilderStep, is_board_only
from oc8.copilot.capabilities import (
    AgentModelSwitch,
    AgentNarrowingSet,
    AgentSkillAssign,
    BoardUpdate,
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
    BuilderBoardDTO,
    BuilderChoiceDTO,
    BuilderDatasetDTO,
    BuilderLimitDTO,
    BuilderTriggerDTO,
    BuilderTriggerSettingDTO,
    LocalizedTextDTO,
)
from oc8.triggers.zones import organisation_timezone

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


async def _target(db: AsyncSession, steps: list[BuilderStep]) -> tuple[uuid.UUID | None, bool]:
    """Where the proposal acts, and whether that is still known: a board-only
    proposal whose board or agent is gone has no department any more -- the
    card still reads, and nobody may apply it."""
    try:
        return await target_department(db, steps), True
    except BuilderInvalid:
        return None, False


def _sees(actor: HumanActor, authority: Authority) -> SeesBoard:
    """Whether the caller may see a board's content NOW, by its department:
    the boards' own rule (§6.8.2) -- a seat there, or `agent:view`
    tenant-wide. A card outlives the seat it was proposed under."""
    everywhere = tenant_wide_read(authority, perm(AGENT, VIEW))
    return lambda department_id: everywhere or actor.scope.may_view(department_id)


def _sees_nothing(_department_id: uuid.UUID) -> bool:
    return False


async def _board_dto(
    db: AsyncSession, proposal: CopilotProposal, steps: list[BuilderStep], sees: SeesBoard
) -> BuilderBoardDTO | None:
    """The board a proposal creates or changes, as the card shows it. What a
    change leaves out is filled from the live board only for a caller who may
    see it; anybody else reads the step's own fields alone."""
    operation = board_step(steps)
    if operation is None:
        return None
    result = proposal.result or {}
    if isinstance(operation, BoardUpdate):
        mode: Literal["create", "update"] = "update"
        board_id: str | None = str(operation.boardId)
    else:
        mode = "create"
        applied = result.get("boardId")
        board_id = applied if isinstance(applied, str) else None
    spec = await board_spec(db, steps, sees=sees) or {}
    datasets = spec.get("datasets") if isinstance(spec.get("datasets"), list) else []
    blocks = spec.get("blocks") if isinstance(spec.get("blocks"), list) else []
    return BuilderBoardDTO(
        board_id=board_id,
        mode=mode,
        title=await board_title(db, steps, sees=sees),
        datasets=[
            BuilderDatasetDTO(
                key=str(ds.get("key", "")),
                label=str(ds.get("label", "")),
                grain=str(ds.get("grain", "")),
                field_count=len(ds.get("fields") or []),
            )
            for ds in datasets or []
            if isinstance(ds, dict)
        ],
        block_count=len(blocks or []),
    )


def _was_submitted(proposal: CopilotProposal) -> bool:
    return (
        isinstance((proposal.result or {}).get("approvalId"), str) and proposal.status in _SUBMITTED
    )


async def _setting_labels(
    db: AsyncSession,
) -> dict[tuple[str | None, str | None, str], dict[str, Any]]:
    """`(source, type, key)` -> the label (and its capa translations) of every
    setting the enabled capas' events declare."""
    from oc8.automation.catalogue import list_installed_automation_events
    from oc8.capas.discovery import find_plugin
    from oc8.capas.i18n import translations_for

    out: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    for event in await list_installed_automation_events(db):
        if not event.config:
            continue
        found = find_plugin(event.source)
        i18n = dict(found.i18n) if found is not None else {}
        for field in event.config:
            out[(event.source, event.type, field.key)] = {
                "label": field.label,
                "label_translations": translations_for(i18n, field.label),
            }
    return out


async def builder_dto(
    db: AsyncSession,
    proposal: CopilotProposal,
    steps: list[BuilderStep],
    *,
    may_create: bool,
    read_only: bool,
    show_mission: bool,
    sees: SeesBoard | None = None,
) -> AgentBuilderDTO:
    """One card. `sees` decides whether a change may be filled from the live
    board (`_sees`); without it, it never is."""
    if sees is None:
        sees = _sees_nothing
    if is_board_only(steps):
        return await _board_only_dto(
            db, proposal, steps, may_create=may_create, read_only=read_only, sees=sees
        )
    create = create_step(steps)
    planned = new_department(steps)
    result = proposal.result or {}
    agent_id = uuid_or_none(result.get("agentId"))
    agent = await db.get(m.Agent, agent_id) if agent_id is not None else None
    if agent is not None and agent.tenant_id != proposal.tenant_id:
        agent = None
    if planned is None:
        department_id: uuid.UUID | None = create.departmentId
    else:
        # Applied: the department it created; before, none exists yet.
        department_id = agent.department_id if agent is not None else None
    department = await db.get(m.Department, department_id) if department_id else None
    mission = create.mission
    model: BuilderChoiceDTO | None = None
    skills: list[BuilderChoiceDTO] = []
    triggers: list[BuilderTriggerDTO] = []
    systems: set[str] = set()
    limits: list[BuilderLimitDTO] = []
    setting_labels: dict[tuple[str | None, str | None, str], dict[str, Any]] | None = None
    for step in steps:
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
            settings: list[BuilderTriggerSettingDTO] = []
            if op.eventConfig:
                if setting_labels is None:
                    setting_labels = await _setting_labels(db)
                settings = [
                    BuilderTriggerSettingDTO(
                        key=key,
                        **setting_labels.get(
                            (op.eventSource, op.eventType, key),
                            {"label": key, "label_translations": {}},
                        ),
                        value=value,
                    )
                    for key, value in op.eventConfig.items()
                ]
            zone = None
            if op.kind == "cron":
                # What create_trigger will read the schedule in.
                zone = op.timezone or await organisation_timezone(db, proposal.tenant_id)
            triggers.append(
                BuilderTriggerDTO(
                    kind=op.kind,
                    cron_expression=op.cronExpression,
                    timezone=zone,
                    event_source=op.eventSource,
                    event_type=op.eventType,
                    # The trigger's task is mission text too.
                    task_text=op.taskText if show_mission else "",
                    settings=settings,
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
    agent_dto: BuilderAgentDTO | None = None
    if agent is not None:
        agent_dto = BuilderAgentDTO(id=str(agent.id), name=agent.name, status=agent.status)
    refusal = result.get("refusal")
    failed_step = result.get("failedStep")
    failed_reason = result.get("failedReason")
    rejected_reason = result.get("rejectedReason")
    is_draft = proposal.status == "draft"
    return AgentBuilderDTO(
        id=str(proposal.id),
        status=proposal.status,
        session_id=str(proposal.session_id) if proposal.session_id else None,
        department_id=str(department_id) if department_id else None,
        department_name=(
            department.name if department is not None else planned.name if planned else ""
        ),
        new_department=planned is not None,
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
        failed_reason=failed_reason if isinstance(failed_reason, str) else None,
        rejected_reason=rejected_reason if isinstance(rejected_reason, str) else None,
        agent=agent_dto,
        notices=[n for n in result.get("notices") or [] if isinstance(n, str)],
        kind="agent",
        board=await _board_dto(db, proposal, steps, sees),
    )


async def _board_only_dto(
    db: AsyncSession,
    proposal: CopilotProposal,
    steps: list[BuilderStep],
    *,
    may_create: bool,
    read_only: bool,
    sees: SeesBoard,
) -> AgentBuilderDTO:
    """A board-only card: no agent of its own, the board's department -- named
    only to a caller who may still see it there (`sees`)."""
    department_id, known = await _target(db, steps)
    if department_id is not None and not sees(department_id):
        department_id = None
    department = await db.get(m.Department, department_id) if department_id else None
    result = proposal.result or {}
    refusal = result.get("refusal")
    failed_step = result.get("failedStep")
    failed_reason = result.get("failedReason")
    rejected_reason = result.get("rejectedReason")
    is_draft = proposal.status == "draft"
    return AgentBuilderDTO(
        id=str(proposal.id),
        status=proposal.status,
        session_id=str(proposal.session_id) if proposal.session_id else None,
        department_id=str(department_id) if department_id else None,
        department_name=department.name if department is not None else "",
        name="",
        role_title="",
        mission="",
        expires_at=proposal.expires_at.isoformat() if proposal.expires_at else None,
        may_create=may_create and is_draft and not read_only,
        # A board or agent that is gone has nowhere to send a request to.
        may_request=is_draft and not read_only and known,
        read_only=read_only,
        refusal=refusal if isinstance(refusal, str) else None,
        failed_step=failed_step if isinstance(failed_step, int) else None,
        failed_reason=failed_reason if isinstance(failed_reason, str) else None,
        rejected_reason=rejected_reason if isinstance(rejected_reason, str) else None,
        kind="board",
        board=await _board_dto(db, proposal, steps, sees),
    )


@router.get("/copilot/builder/{proposal_id}", response_model=AgentBuilderDTO)
async def get_builder(
    request: Request, proposal_id: uuid.UUID, db: DbSession, actor: _Actor
) -> AgentBuilderDTO:
    proposal, owner, steps = await _load(db, actor, proposal_id)
    # None for a new department: only who reads and decides everywhere. For a
    # board-only proposal, the board's (or its agent's) department, read live.
    department_id, known = await _target(db, steps)
    # A draft, a superseded one, one never sent: the member's alone. Once it is
    # in a queue, whoever may view that department reads it (Ruling A4).
    if not owner and not (_was_submitted(proposal) and actor.scope.may_view(department_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    authority = await authority_for_principal(request, db, actor.principal)
    may_create = known and await may_apply(db, authority, actor.scope, steps)
    # The approver is whoever the approvals funnel would let decide it.
    approver = actor.scope.may_decide(department_id) and may_create
    dto = await builder_dto(
        db,
        proposal,
        steps,
        may_create=owner and may_create,
        read_only=not owner,
        show_mission=owner or approver,
        sees=_sees(actor, authority),
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
    # Read before applying: a refusal commits, and the notice that follows
    # names the board by this title (`builder_notice` reads no board).
    sees = _sees(actor, authority)
    board_only = is_board_only(steps)
    title = await board_title(db, steps, sees=sees) if board_only else ""
    try:
        # A board-only proposal is decided in this department; apply reads it
        # again under the board's lock and refuses one that moved meanwhile.
        decided = await target_department(db, steps) if board_only else None
        allowed = await may_apply(db, authority, actor.scope, steps)
    except BuilderInvalid as exc:
        # The board or its agent is gone: nothing to apply.
        await db.commit()
        raise _refused(exc) from None
    if not allowed:
        await db.commit()  # keep a lazy expiry
        if not is_board_only(steps) and await target_department(db, steps) is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires {perm(DEPARTMENT, MANAGE)} and {perm(AGENT, MANAGE)}",
            )
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
            decided_department=decided,
        )
    except BuilderInvalid as exc:
        # The refusal (and its step) stays on the proposal for the card.
        await db.commit()
        # And the Copilot hears of it: one notice turn in this chat, so it
        # explains the refusal and fixes the proposal or says what to do.
        await tell_copilot_best_effort(
            db,
            tenant_id=actor.principal.tenant_id,
            proposal_id=proposal.id,
            steps=steps,
            member=actor.member,
            operator_role=actor.principal.role,
            board_title=title,
        )
        raise _refused(exc) from None
    await announce_builder(db, proposal)
    dto = await builder_dto(
        db, proposal, steps, may_create=True, read_only=False, show_mission=True, sees=sees
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
            boards_tenant_wide=tenant_wide_read(authority, perm(AGENT, VIEW)),
            now=_now(),
        )
    except BuilderInvalid as exc:
        await db.commit()
        raise _refused(exc) from None
    _department_id, known = await _target(db, steps)
    dto = await builder_dto(
        db,
        proposal,
        steps,
        may_create=known and await may_apply(db, authority, actor.scope, steps),
        read_only=False,
        show_mission=True,
        sees=_sees(actor, authority),
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
