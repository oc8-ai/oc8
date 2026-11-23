"""Personal departments: the member's own routes, then the administrator's.

Every member route is gated by `department:own` (tenant-wide, like
`connection:own`) and acts on the CALLER's own personal department only -- there
is no department id in any path, so nobody can name someone else's. The member
is resolved with `scope_for_principal(..., upsert=True)`; every department read
goes through `departments/personal.py`, which looks a personal department up by
its owner, never by an id the caller supplies.

The admin routes (the on/off switch, the default frame and budget, the
"Personal" list and follow-default) sit behind tenant-wide `settings:*`,
`department:manage` and `budget:*`. They see a personal department's metadata
only, and every department or agent they touch is loaded by
`departments/personal.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1._serializers import department_to_dto
from oc8.api.v1.departments import (
    DepartmentToolsDTO,
    DepartmentToolsWriteRequest,
    frame_tools_of,
)
from oc8.audit import append_event
from oc8.auth.principal import Principal
from oc8.authz.permissions import (
    BUDGET,
    DEPARTMENT,
    DEPARTMENT_OWN,
    MANAGE,
    SETTINGS,
    VIEW,
    perm,
)
from oc8.authz.scope import scope_for_principal
from oc8.capas.service import PluginError, instantiate_agent, template_scope
from oc8.departments.frames import FrameChangeRefused, deviation_counts, validate_frame_tools
from oc8.departments.personal import (
    BUDGET_KEY,
    ENABLED_KEY,
    FRAME_KEY,
    MAX_AGENTS_KEY,
    PersonalDepartmentRefused,
    PersonalDepartmentsDisabled,
    PersonalDepartmentSummary,
    default_frame_agents,
    follow_default,
    get_or_create_personal_department,
    owned_personal_department,
    personal_defaults,
    personal_department_summaries,
    resync_budgets,
    resync_frames,
    update_personal_settings,
)
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import DepartmentDTO

router = APIRouter()

_OWN = [Depends(require_permission(DEPARTMENT_OWN))]

#: The longest agent name the API accepts anywhere else (`AgentRenameRequest`).
_AGENT_NAME_MAX_LENGTH = 200


class PersonalDepartmentMineDTO(CamelModel):
    #: Whether the tenant lets members hire into a personal department now.
    enabled: bool
    #: The caller's personal department, once the first hire created it. Kept
    #: when the feature is switched off later. Its agents come from
    #: `GET /agents?departmentId=`.
    department: DepartmentDTO | None


class PersonalTemplateDTO(CamelModel):
    capa_id: str
    name: str
    label: str
    summary: str


class HirePersonalRequest(CamelModel):
    capa_id: uuid.UUID
    #: Optional; trimmed, and an empty name falls back to the template's own.
    #: The member names the agent and nothing else -- every other field comes
    #: from the template.
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=_AGENT_NAME_MAX_LENGTH)]
        | None
    ) = None


class HirePersonalResponse(CamelModel):
    agent_id: str
    department_id: str


async def _caller_member(db: AsyncSession, principal: Principal) -> m.OrgMember:
    try:
        member, _scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this token does not belong to a person with an account"
        ) from exc
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no member record for this caller")
    return member


def _template_label(manifest: dict[str, Any]) -> str:
    spec = manifest.get("agent_template") or {}
    return str(manifest.get("label") or spec.get("name") or manifest.get("name", ""))


@router.get(
    "/personal-department",
    response_model=PersonalDepartmentMineDTO,
    dependencies=_OWN,
)
async def get_my_personal_department(
    db: DbSession, principal: CurrentPrincipal
) -> PersonalDepartmentMineDTO:
    member = await _caller_member(db, principal)
    defaults = await personal_defaults(db, tenant_id=principal.tenant_id)
    dept = await owned_personal_department(db, tenant_id=principal.tenant_id, member_id=member.id)
    return PersonalDepartmentMineDTO(
        enabled=defaults.enabled,
        department=department_to_dto(dept) if dept is not None else None,
    )


@router.get(
    "/personal-department/templates",
    response_model=list[PersonalTemplateDTO],
    dependencies=_OWN,
)
async def list_personal_templates(
    db: DbSession, principal: CurrentPrincipal
) -> list[PersonalTemplateDTO]:
    """The agent templates a member may hire: enabled by an administrator and
    declared `scope = "personal"`."""
    await _caller_member(db, principal)
    rows = (
        await db.execute(
            select(m.Capa, m.CapaVersion)
            .join(
                m.CapaInstallation,
                (m.CapaInstallation.capa_id == m.Capa.id)
                & (m.CapaInstallation.tenant_id == m.Capa.tenant_id),
            )
            # The version the admin ENABLED, never a newer one only installed
            # since (`Capa.current_version_id`).
            .join(m.CapaVersion, m.CapaVersion.id == m.CapaInstallation.version_id)
            .where(
                m.Capa.tenant_id == principal.tenant_id,
                m.CapaInstallation.status == "enabled",
            )
            .order_by(m.Capa.name)
        )
    ).all()
    out: list[PersonalTemplateDTO] = []
    for capa, version in rows:
        mf = version.manifest or {}
        if mf.get("type") != "agent_template" or template_scope(mf) != "personal":
            continue
        out.append(
            PersonalTemplateDTO(
                capa_id=str(capa.id),
                name=capa.name,
                label=_template_label(mf),
                summary=str(mf.get("summary", "")),
            )
        )
    return out


@router.post(
    "/personal-department/agents",
    response_model=HirePersonalResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_OWN,
)
async def hire_personal_agent(
    body: HirePersonalRequest, db: DbSession, principal: CurrentPrincipal
) -> HirePersonalResponse:
    capa = await db.get(m.Capa, body.capa_id)
    if capa is None or capa.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    installation = (
        await db.execute(
            select(m.CapaInstallation).where(
                m.CapaInstallation.tenant_id == principal.tenant_id,
                m.CapaInstallation.capa_id == capa.id,
            )
        )
    ).scalar_one_or_none()
    if installation is None or installation.status != "enabled":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not enabled")
    # The version the admin enabled -- not `Capa.current_version_id`, which a
    # later install moves without anybody enabling it.
    if installation.version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin or version not found")
    version = await db.get(m.CapaVersion, installation.version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin or version not found")
    mf = version.manifest or {}
    if mf.get("type") != "agent_template" or template_scope(mf) != "personal":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, {"error": "template_not_personal"}
        )

    # Checked here, and not only inside `get_or_create_personal_department`:
    # that returns an existing department even when the feature has since been
    # switched off, and a switched-off feature takes no new hires at all.
    defaults = await personal_defaults(db, tenant_id=principal.tenant_id)
    if not defaults.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"error": "personal_departments_disabled"})
    member = await _caller_member(db, principal)
    try:
        dept = await get_or_create_personal_department(db, member=member)
    except PersonalDepartmentsDisabled as exc:  # pragma: no cover - checked above
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, {"error": "personal_departments_disabled"}
        ) from exc
    try:
        # Plan ruling 4: a personal hire is exempt from the tenant's
        # `require_hire_approval`. The administrator who enabled this
        # personal template's capa has already approved it; gating it again
        # would make the owner -- `dept_approver` in their own department --
        # approve their own hire.
        agent = await instantiate_agent(
            db,
            tenant_id=principal.tenant_id,
            version=version,
            department_id=dept.id,
            name=body.name or None,
            hire_gate=False,
            owner_hire=True,
        )
    except PersonalDepartmentRefused as exc:  # the cap, or the owner left (`instantiate_agent`)
        raise HTTPException(status.HTTP_409_CONFLICT, {"error": exc.error}) from exc
    except PluginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.created",
        resource={"agent_id": str(agent.id), "department_id": str(dept.id), "personal": True},
        principal=principal,
    )
    return HirePersonalResponse(agent_id=str(agent.id), department_id=str(dept.id))


# --- Administration -----------------------------------------------------------


class PersonalDepartmentsSetting(CamelModel):
    enabled: bool
    #: How many live agents a member may hire into their personal department.
    max_agents: int


class PersonalDepartmentsSettingWrite(CamelModel):
    enabled: bool
    #: Optional; left out, the stored cap is kept.
    max_agents: Annotated[int, Field(strict=True, ge=1, le=1000)] | None = None


class PersonalBudgetDefaults(CamelModel):
    soft_limit_tokens: Annotated[int, Field(ge=0)] | None = None
    hard_limit_tokens: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _soft_not_above_hard(self) -> PersonalBudgetDefaults:
        soft, hard = self.soft_limit_tokens, self.hard_limit_tokens
        if soft is not None and hard is not None and soft > hard:
            raise ValueError("softLimitTokens must not exceed hardLimitTokens")
        return self


class PersonalDepartmentAdminDTO(CamelModel):
    """A personal department as an administrator sees it: metadata only."""

    id: str
    owner_member_id: str
    owner_name: str
    agent_count: int
    follows_default_frame: bool
    follows_default_budget: bool
    created_at: dt.datetime


def _admin_dto(row: PersonalDepartmentSummary) -> PersonalDepartmentAdminDTO:
    return PersonalDepartmentAdminDTO(
        id=str(row.id),
        owner_member_id=str(row.owner_member_id),
        owner_name=row.owner_name,
        agent_count=row.agent_count,
        follows_default_frame=row.follows_default_frame,
        follows_default_budget=row.follows_default_budget,
        created_at=row.created_at,
    )


async def _default_frame_dto(db: AsyncSession, tenant_id: uuid.UUID) -> DepartmentToolsDTO:
    tools = (await personal_defaults(db, tenant_id=tenant_id)).frame_tools
    agents = await default_frame_agents(db, tenant_id=tenant_id)
    return DepartmentToolsDTO(
        tools=tools,
        deviation_counts=deviation_counts(tools, agents),
        agent_count=len(agents),
    )


async def _write_settings(
    db: AsyncSession,
    principal: Principal,
    updates: dict[str, Any],
    resource: dict[str, Any],
) -> None:
    organization = await update_personal_settings(
        db, tenant_id=principal.tenant_id, updates=updates
    )
    if organization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="organization.settings.updated",
        resource={"organization_id": str(organization.id), "by": principal.subject, **resource},
        principal=principal,
    )


@router.get(
    "/settings/personal-departments",
    response_model=PersonalDepartmentsSetting,
    dependencies=[Depends(require_permission(perm(SETTINGS, VIEW)))],
)
async def get_personal_departments_setting(
    db: DbSession, principal: CurrentPrincipal
) -> PersonalDepartmentsSetting:
    defaults = await personal_defaults(db, tenant_id=principal.tenant_id)
    return PersonalDepartmentsSetting(enabled=defaults.enabled, max_agents=defaults.max_agents)


@router.put(
    "/settings/personal-departments",
    response_model=PersonalDepartmentsSetting,
    dependencies=[Depends(require_permission(perm(SETTINGS, MANAGE)))],
)
async def put_personal_departments_setting(
    body: PersonalDepartmentsSettingWrite, db: DbSession, principal: CurrentPrincipal
) -> PersonalDepartmentsSetting:
    # Left out, `maxAgents` keeps the cap in force -- which is what gets audited.
    max_agents = (
        body.max_agents
        if body.max_agents is not None
        else (await personal_defaults(db, tenant_id=principal.tenant_id)).max_agents
    )
    updates: dict[str, Any] = {ENABLED_KEY: body.enabled}
    if body.max_agents is not None:
        updates[MAX_AGENTS_KEY] = body.max_agents
    await _write_settings(
        db,
        principal,
        updates,
        {"setting": ENABLED_KEY, "enabled": body.enabled, "max_agents": max_agents},
    )
    return PersonalDepartmentsSetting(enabled=body.enabled, max_agents=max_agents)


@router.get(
    "/personal-departments/default-frame",
    response_model=DepartmentToolsDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def get_personal_default_frame(
    db: DbSession, principal: CurrentPrincipal
) -> DepartmentToolsDTO:
    """The tools every personal department starts with. `agentCount` counts the
    agents of the personal departments that follow this default."""
    return await _default_frame_dto(db, principal.tenant_id)


@router.put(
    "/personal-departments/default-frame",
    response_model=DepartmentToolsDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def put_personal_default_frame(
    body: DepartmentToolsWriteRequest, db: DbSession, principal: CurrentPrincipal
) -> DepartmentToolsDTO:
    try:
        new_tools = await validate_frame_tools(
            db, tenant_id=principal.tenant_id, tools=frame_tools_of(body)
        )
        resynced = await resync_frames(db, tenant_id=principal.tenant_id, new_tools=new_tools)
    except FrameChangeRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.detail) from exc
    await _write_settings(
        db, principal, {FRAME_KEY: new_tools}, {"setting": FRAME_KEY, "resynced": resynced}
    )
    return await _default_frame_dto(db, principal.tenant_id)


@router.get(
    "/personal-departments/default-budget",
    response_model=PersonalBudgetDefaults,
    dependencies=[Depends(require_permission(perm(BUDGET, VIEW)))],
)
async def get_personal_default_budget(
    db: DbSession, principal: CurrentPrincipal
) -> PersonalBudgetDefaults:
    defaults = await personal_defaults(db, tenant_id=principal.tenant_id)
    return PersonalBudgetDefaults(
        soft_limit_tokens=defaults.soft_limit_tokens,
        hard_limit_tokens=defaults.hard_limit_tokens,
    )


@router.put(
    "/personal-departments/default-budget",
    response_model=PersonalBudgetDefaults,
    dependencies=[Depends(require_permission(perm(BUDGET, MANAGE)))],
)
async def put_personal_default_budget(
    body: PersonalBudgetDefaults, db: DbSession, principal: CurrentPrincipal
) -> PersonalBudgetDefaults:
    soft, hard = body.soft_limit_tokens, body.hard_limit_tokens
    resynced = await resync_budgets(
        db, tenant_id=principal.tenant_id, soft_limit_tokens=soft, hard_limit_tokens=hard
    )
    await _write_settings(
        db,
        principal,
        {BUDGET_KEY: {"soft_limit_tokens": soft, "hard_limit_tokens": hard}},
        {
            "setting": BUDGET_KEY,
            "soft_limit_tokens": soft,
            "hard_limit_tokens": hard,
            "resynced": resynced,
        },
    )
    return body


@router.get(
    "/personal-departments",
    response_model=list[PersonalDepartmentAdminDTO],
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def list_personal_departments(
    db: DbSession, principal: CurrentPrincipal
) -> list[PersonalDepartmentAdminDTO]:
    rows = await personal_department_summaries(db, tenant_id=principal.tenant_id)
    return [_admin_dto(row) for row in rows]


@router.post(
    "/personal-departments/{dept_id}/follow-default",
    response_model=PersonalDepartmentAdminDTO,
    dependencies=[
        Depends(require_permission(perm(DEPARTMENT, MANAGE))),
        Depends(require_permission(perm(BUDGET, MANAGE))),
    ],
)
async def follow_personal_default(
    dept_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> PersonalDepartmentAdminDTO:
    """Drop both overrides of one personal department and re-apply the tenant's
    current default frame and budget to it."""
    try:
        dept = await follow_default(db, tenant_id=principal.tenant_id, department_id=dept_id)
    except FrameChangeRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.detail) from exc
    if dept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "personal department not found")
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="department.personal.follow_default",
        resource={"department_id": str(dept_id), "by": principal.subject},
        principal=principal,
    )
    rows = await personal_department_summaries(
        db, tenant_id=principal.tenant_id, department_id=dept_id
    )
    return _admin_dto(rows[0])
