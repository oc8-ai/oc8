"""Personal departments: the member's own routes.

Every route here is gated by `department:own` (tenant-wide, like
`connection:own`) and acts on the CALLER's own personal department only -- there
is no department id in any path, so nobody can name someone else's. The member
is resolved with `scope_for_principal(..., upsert=True)`; every department read
goes through `departments/personal.py`, which looks a personal department up by
its owner, never by an id the caller supplies.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1._serializers import department_to_dto
from oc8.audit import append_event
from oc8.auth.principal import Principal
from oc8.authz.permissions import DEPARTMENT_OWN
from oc8.authz.scope import scope_for_principal
from oc8.capas.service import PluginError, instantiate_agent, template_scope
from oc8.departments.personal import (
    PersonalDepartmentsDisabled,
    get_or_create_personal_department,
    owned_personal_department,
    personal_defaults,
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
            .join(m.CapaVersion, m.CapaVersion.id == m.Capa.current_version_id)
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
    if capa is None or capa.current_version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin or version not found")
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
    version = await db.get(m.CapaVersion, capa.current_version_id)
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
        )
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
