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
import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ConfigDict, Field, StringConstraints, ValidationError, model_validator
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
from oc8.capas.manifest import TemplateAgent
from oc8.capas.service import PluginError, instantiate_agent, template_scope
from oc8.departments.frames import FrameChangeRefused, deviation_counts, validate_frame_tools
from oc8.departments.owner_confirmation import (
    ConfirmationChanged,
    NotTheOwner,
    PendingConfirmation,
    confirm_pending,
    pending_confirmation,
)
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
    lock_owned_frame,
    owned_personal_department,
    personal_defaults,
    personal_department_summaries,
    resync_budgets,
    resync_frames,
    update_personal_settings,
)
from oc8.departments.personal_hire import (
    ChosenLogin,
    HireBindingRefused,
    TemplateConnection,
    hire_offer,
    resolve_hire_bindings,
    template_connections,
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


class TemplateConnectionDTO(CamelModel):
    """A capa a personal template connects to, and whether a hire may bind it
    now (plan Ruling 10)."""

    integration: str
    label: str
    #: "credential" or "oauth"; "" for a capa this organisation has not installed.
    kind: str
    provider: str | None
    credential_type: str | None
    available: bool


class HireGrantsDTO(CamelModel):
    """What a hire grants the agent, shown before the member confirms it."""

    #: The frame tools the agent may use.
    tools: list[str]
    #: The skills it runs with.
    skills: list[str]


class PersonalTemplateDTO(CamelModel):
    capa_id: str
    name: str
    label: str
    summary: str
    #: Pins this offer: the hire answers 409 `hire_changed` when the template
    #: version, its setup, the frame tools or the skills changed since.
    hire_token: str
    grants: HireGrantsDTO
    #: Left out for a template that connects to nothing.
    #: The member chooses a login for one of these at hire.
    connections_any: list[TemplateConnectionDTO] = []
    #: The member may add a login for each of these.
    connections_optional: list[TemplateConnectionDTO] = []


class HireConnection(CamelModel):
    """The member's own login for one capa the template names."""

    model_config = ConfigDict(extra="forbid")

    integration: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    credential_id: uuid.UUID | None = None
    oauth_connection_id: uuid.UUID | None = None

    def chosen(self) -> ChosenLogin:
        return ChosenLogin(self.integration, self.credential_id, self.oauth_connection_id)


class HirePersonalRequest(CamelModel):
    model_config = ConfigDict(extra="forbid")

    capa_id: uuid.UUID
    #: The `hireToken` of the template as the list showed it (Ruling T8-I1).
    hire_token: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    #: Optional; trimmed, and an empty name falls back to the template's own.
    #: The member names the agent and the logins it uses -- every other field
    #: comes from the template.
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=_AGENT_NAME_MAX_LENGTH)]
        | None
    ) = None
    #: The login for one of the template's `connections_any`.
    connection: HireConnection | None = None
    #: Logins for any of the template's `connections_optional`.
    optional_connections: list[HireConnection] = Field(default_factory=list, max_length=20)
    #: Binding a login of one's own shares it with the agent: required with
    #: any login, as for `PUT /agents/{id}/tool-access/{key}`.
    confirm_share: bool = False


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


def _template_agent(manifest: dict[str, Any]) -> TemplateAgent | None:
    """The stored template's agent, read as the manifest declares it; None
    when it does not parse (a manifest stored before a stricter schema)."""
    try:
        return TemplateAgent.model_validate(manifest.get("agent_template") or {})
    except ValidationError:
        return None


async def _hire_frame_tools(
    db: AsyncSession, *, tenant_id: uuid.UUID, dept: m.Department | None
) -> dict[str, Any]:
    """The frame tools a hire binds against (Ruling 10): the member's
    personal department as applied, or the tenant default before its first
    hire."""
    if dept is not None:
        tools = (dept.frame or {}).get("tools")
        return dict(tools) if isinstance(tools, dict) else {}
    return (await personal_defaults(db, tenant_id=tenant_id)).frame_tools


def _connection_dtos(rows: list[TemplateConnection]) -> list[TemplateConnectionDTO]:
    return [
        TemplateConnectionDTO(
            integration=row.integration,
            label=row.label,
            kind=row.kind,
            provider=row.provider,
            credential_type=row.credential_type,
            available=row.available,
        )
        for row in rows
    ]


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
    # A template without connections answers as before PR 2: no empty lists.
    response_model_exclude_unset=True,
    dependencies=_OWN,
)
async def list_personal_templates(
    db: DbSession, principal: CurrentPrincipal
) -> list[PersonalTemplateDTO]:
    """The agent templates a member may hire: enabled by an administrator and
    declared `scope = "personal"`, each with the capas it connects to and
    whether a hire may bind them now."""
    member = await _caller_member(db, principal)
    dept = await owned_personal_department(db, tenant_id=principal.tenant_id, member_id=member.id)
    frame_tools = await _hire_frame_tools(db, tenant_id=principal.tenant_id, dept=dept)
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
        spec = _template_agent(mf)
        if spec is None:
            continue  # the hire refuses it too
        offer = await hire_offer(
            db,
            version=version,
            spec=spec,
            frame_tools=frame_tools,
            department_id=dept.id if dept is not None else None,
        )
        connections: dict[str, list[TemplateConnectionDTO]] = {}
        if spec.connections_any or spec.connections_optional:
            connections["connections_any"] = _connection_dtos(
                await template_connections(db, frame_tools=frame_tools, names=spec.connections_any)
            )
            connections["connections_optional"] = _connection_dtos(
                await template_connections(
                    db, frame_tools=frame_tools, names=spec.connections_optional
                )
            )
        out.append(
            PersonalTemplateDTO(
                capa_id=str(capa.id),
                name=capa.name,
                label=_template_label(mf),
                summary=str(mf.get("summary", "")),
                hire_token=offer.token,
                grants=HireGrantsDTO(tools=offer.tools, skills=offer.skills),
                **connections,
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
    spec = _template_agent(mf)
    if spec is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the template does not parse")
    # Every choice is checked before anything is written, against the frame
    # the department has (or will start with): a refused hire creates no
    # department.
    unprocessable = status.HTTP_422_UNPROCESSABLE_CONTENT
    try:
        bindings = await resolve_hire_bindings(
            db,
            member=member,
            frame_tools=await _hire_frame_tools(
                db,
                tenant_id=principal.tenant_id,
                dept=await owned_personal_department(
                    db, tenant_id=principal.tenant_id, member_id=member.id
                ),
            ),
            spec=spec,
            connection=body.connection.chosen() if body.connection is not None else None,
            optional=[c.chosen() for c in body.optional_connections],
        )
    except HireBindingRefused as exc:
        raise HTTPException(unprocessable, {"error": exc.error}) from exc
    if bindings and not body.confirm_share:
        raise HTTPException(unprocessable, {"error": "confirm_share_required"})
    try:
        dept = await get_or_create_personal_department(db, member=member)
    except PersonalDepartmentsDisabled as exc:  # pragma: no cover - checked above
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, {"error": "personal_departments_disabled"}
        ) from exc
    # Ruling T8-I1: the hire confirms what the member was shown. Checked
    # against the department as locked (a frame write takes the same lock),
    # so a change committed since the list answers 409 and the transaction,
    # the department created just now included, rolls back.
    await lock_owned_frame(db, dept)
    frame_tools = await _hire_frame_tools(db, tenant_id=principal.tenant_id, dept=dept)

    async def check_offer() -> None:
        offer = await hire_offer(
            db, version=version, spec=spec, frame_tools=frame_tools, department_id=dept.id
        )
        if not hmac.compare_digest(offer.token, body.hire_token):
            raise HTTPException(status.HTTP_409_CONFLICT, {"error": "hire_changed"})

    await check_offer()
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
            owner_hire=True,
            hired_by=member.id,
            bindings=bindings,
        )
    except PersonalDepartmentRefused as exc:  # the cap, or the owner left (`instantiate_agent`)
        raise HTTPException(status.HTTP_409_CONFLICT, {"error": exc.error}) from exc
    except PluginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # Ruling T8-R1: and once more after the stamps. Skills and setup values
    # are not under the department lock, and a stamp records what it reads
    # (read committed): one committed since the check above would count as
    # confirmed by the member. Checked last, so the stamps can only hold
    # what the member was shown; a mismatch rolls the whole hire back.
    await check_offer()

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


class ConfirmationToolDTO(CamelModel):
    """A frame tool an administrator loosened: both policies in full, as the
    runtime reads them (`enabled`, `read`, `modify`, `only`, `limits`,
    `approval_eur`, `approval_actions`, `conditions`, `connection_id`)."""

    key: str
    #: What applies now; None when the department grants nothing for it.
    applied: dict[str, Any] | None
    #: What applies once the owner confirms.
    pending: dict[str, Any] | None


class ConfirmationSkillDTO(CamelModel):
    """A skill held back from the agents, as the model would read it."""

    name: str
    description: str | None
    #: "new" (never confirmed) or "changed" (version, overrides or text).
    status: str


class ConfirmationLoginDTO(CamelModel):
    """A login a setup uses: its name, never a secret."""

    name: str
    owned_by_owner: bool


class ConfirmationSetupDTO(CamelModel):
    """A tool's setup as the agent's runs use it, without secrets."""

    tool: str
    server_url: str | None
    transport: str | None
    scopes: Any
    #: The setup's settings, by name only: their values are never shown.
    config_keys: list[str]
    logins: list[ConfirmationLoginDTO]


class ConfirmationAgentDTO(CamelModel):
    """An agent whose earlier confirmation no longer holds."""

    agent_id: str
    name: str
    #: What changed: "core" (its version, a setup, or the department's goal),
    #: "frame" (the department's frame but its tools), "tools", "skills",
    #: "threshold" (its own approval threshold).
    changes: list[str]
    #: Its own approval threshold now (EUR), or None.
    approval_threshold: Any
    #: With "tools" or "threshold": every tool it may use now, as its grant
    #: records it (the policy, plus `frame_extra`/`agent_extra`: fields the
    #: policy does not read, such as a default login).
    tools: dict[str, Any] | None = None
    #: With "core": the setups its runs use now.
    setup: list[ConfirmationSetupDTO] | None = None


class ConfirmationDepartmentDTO(CamelModel):
    """The department as the owner would confirm it."""

    goal: str
    #: Every frame key but the tools.
    frame: dict[str, Any]
    #: What the department's tools reset to.
    frame_capa_defaults: dict[str, Any] | None


class PersonalConfirmationDTO(CamelModel):
    """What waits for the owner's confirmation in their personal department,
    shown as it is now in full (Ruling T9-I2).

    Each part shows what waits now, not a history: an administrator's later
    write that only tightens the frame replaces an earlier held loosening, so
    a loosening listed before may be gone without the owner confirming it."""

    #: Whether anything waits at all.
    needed: bool
    #: Pins this summary: the confirm answers 409 `confirmation_changed` when
    #: anything it would accept changed since, listed here or not.
    token: str
    #: The frame tools an administrator loosened; they apply once confirmed.
    tools: list[ConfirmationToolDTO]
    #: The skills held back from the agents until the owner confirms them.
    skills: list[ConfirmationSkillDTO]
    #: The agents whose earlier confirmation no longer holds.
    agents: list[ConfirmationAgentDTO]
    #: Set when an agent's change may come from the department ("core",
    #: "frame"): its goal and frame as they are now.
    department: ConfirmationDepartmentDTO | None


class PersonalConfirmRequest(CamelModel):
    model_config = ConfigDict(extra="forbid")

    #: The `token` of the summary as the owner was shown it.
    token: Annotated[str, StringConstraints(min_length=1, max_length=128)]


def _confirmation_dto(summary: PendingConfirmation) -> PersonalConfirmationDTO:
    return PersonalConfirmationDTO(
        needed=summary.needed,
        token=summary.token,
        tools=[
            ConfirmationToolDTO(key=t.key, applied=t.applied, pending=t.pending)
            for t in summary.tools
        ],
        skills=[
            ConfirmationSkillDTO(name=s.name, description=s.description, status=s.status)
            for s in summary.skills
        ],
        agents=[
            ConfirmationAgentDTO(
                agent_id=str(a.agent_id),
                name=a.name,
                changes=a.changes,
                approval_threshold=a.approval_threshold,
                tools=a.tools,
                setup=(
                    [
                        ConfirmationSetupDTO(
                            tool=v.tool,
                            server_url=v.server_url,
                            transport=v.transport,
                            scopes=v.scopes,
                            config_keys=v.config_keys,
                            logins=[
                                ConfirmationLoginDTO(name=x.name, owned_by_owner=x.owned_by_owner)
                                for x in v.logins
                            ],
                        )
                        for v in a.setup
                    ]
                    if a.setup is not None
                    else None
                ),
            )
            for a in summary.agents
        ],
        department=(
            ConfirmationDepartmentDTO(
                goal=summary.department.goal,
                frame=summary.department.frame,
                frame_capa_defaults=summary.department.frame_capa_defaults,
            )
            if summary.department is not None
            else None
        ),
    )


async def _own_department(
    db: AsyncSession, principal: Principal
) -> tuple[m.OrgMember, m.Department]:
    """The caller's own personal department, or 404: an administrator, a
    colleague -- anybody without one of their own -- reaches nothing here."""
    member = await _caller_member(db, principal)
    dept = await owned_personal_department(db, tenant_id=principal.tenant_id, member_id=member.id)
    if dept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no personal department")
    return member, dept


@router.get(
    "/personal-department/confirmation",
    response_model=PersonalConfirmationDTO,
    dependencies=_OWN,
)
async def get_personal_confirmation(
    db: DbSession, principal: CurrentPrincipal
) -> PersonalConfirmationDTO:
    """What waits for the caller's confirmation in their own personal
    department (`departments.owner_confirmation`)."""
    _member, dept = await _own_department(db, principal)
    return _confirmation_dto(await pending_confirmation(db, dept))


@router.post(
    "/personal-department/confirm",
    response_model=PersonalConfirmationDTO,
    dependencies=_OWN,
)
async def confirm_personal_department(
    body: PersonalConfirmRequest, db: DbSession, principal: CurrentPrincipal
) -> PersonalConfirmationDTO:
    """The owner confirms everything the summary with `token` showed them;
    409 `confirmation_changed` when it changed since, with nothing applied."""
    member, dept = await _own_department(db, principal)
    try:
        summary = await confirm_pending(db, dept, member=member, token=body.token)
    except ConfirmationChanged as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"error": "confirmation_changed"}) from exc
    except NotTheOwner as exc:  # pragma: no cover - `_own_department` is the owner's
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no personal department") from exc
    except FrameChangeRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.detail) from exc
    return _confirmation_dto(summary)


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
    """A personal department as an administrator sees it: metadata only.

    `pending_confirmation` covers held frame tools only (Ruling FR-I3): a
    skill held back for the owner, or an agent whose owner confirmation no
    longer holds, does not set it. The owner's card lists all of it."""

    id: str
    owner_member_id: str
    owner_name: str
    # False once the owner has left (or the member row is gone): the UI says
    # "former member" rather than guessing from an empty name.
    owner_active: bool
    agent_count: int
    follows_default_frame: bool
    follows_default_budget: bool
    created_at: dt.datetime
    #: An administrator's loosening of the frame tools waits for the owner to
    #: confirm it (`departments.personal.apply_held_tools`). Only that it
    #: waits: what waits is the owner's (`GET /personal-department/confirmation`).
    pending_confirmation: bool


def _admin_dto(row: PersonalDepartmentSummary) -> PersonalDepartmentAdminDTO:
    return PersonalDepartmentAdminDTO(
        id=str(row.id),
        owner_member_id=str(row.owner_member_id),
        owner_name=row.owner_name,
        owner_active=row.owner_active,
        agent_count=row.agent_count,
        follows_default_frame=row.follows_default_frame,
        follows_default_budget=row.follows_default_budget,
        created_at=row.created_at,
        pending_confirmation=row.pending_confirmation,
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
