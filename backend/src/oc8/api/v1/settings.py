"""Tenant settings for the signed-in organization and approval policies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from oc8 import models as m
from oc8.agents.hire import require_hire_approval, set_require_hire_approval
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.authz.permissions import MANAGE, SETTINGS, VIEW, perm
from oc8.copilot.followups import max_active_followups
from oc8.credentials.service import list_credentials
from oc8.schemas.base import CamelModel

router = APIRouter()


class HireApprovalSetting(BaseModel):
    enabled: bool


class CopilotSettings(CamelModel):
    max_active_followups: int = Field(ge=1, le=100)


class OrganizationSettings(BaseModel):
    id: str
    name: str
    slug: str
    tier: str
    region: str
    #: Which of this tenant's `smtp_server` credentials outbound mail
    #: actually goes through (`oc8.mail.send.active_smtp_credential`).
    #: `None` means no mail server is configured, and every `send_mail`
    #: for this tenant quietly returns False.
    active_smtp_credential_id: str | None = None


class OrganizationSettingsUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    region: str = Field(min_length=1, max_length=80)
    #: Three-state on purpose, read via `model_fields_set` below: OMITTED
    #: leaves the current pointer alone, an explicit `null` clears it, an id
    #: selects that credential. Treating omitted as null instead would mean
    #: the existing settings form -- which posts only {name, region}
    #: (frontend/src/lib/hooks.ts `useUpdateOrganizationSettings`) -- silently
    #: disconnected the mail server every time an admin renamed the org, and
    #: password reset would stop working with nothing on screen to say why.
    active_smtp_credential_id: str | None = None

    @field_validator("name", "region")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


def _organization_to_dto(organization: m.Organization) -> OrganizationSettings:
    return OrganizationSettings(
        id=str(organization.id),
        name=organization.name,
        slug=organization.slug,
        tier=organization.tier,
        region=organization.region,
        active_smtp_credential_id=organization.settings.get("active_smtp_credential_id"),
    )


@router.get(
    "/settings/organization",
    response_model=OrganizationSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, VIEW)))],
)
async def get_organization_settings(
    db: DbSession, principal: CurrentPrincipal
) -> OrganizationSettings:
    organization = await db.get(m.Organization, principal.tenant_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return _organization_to_dto(organization)


@router.put(
    "/settings/organization",
    response_model=OrganizationSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, MANAGE)))],
)
async def put_organization_settings(
    body: OrganizationSettingsUpdate,
    db: DbSession,
    principal: CurrentPrincipal,
) -> OrganizationSettings:
    organization = await db.get(m.Organization, principal.tenant_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    organization.name = body.name
    organization.region = body.region
    if "active_smtp_credential_id" in body.model_fields_set:
        if body.active_smtp_credential_id is not None:
            # Server-side filter (credentials/service.py:108-115), so this
            # never walks every credential the tenant owns.
            smtp_credentials = await list_credentials(
                db, tenant_id=principal.tenant_id, credential_type="smtp_server"
            )
            if not any(str(c.id) == body.active_smtp_credential_id for c in smtp_credentials):
                raise HTTPException(status_code=404, detail="smtp_server credential not found")
        organization.settings = {
            **organization.settings,
            "active_smtp_credential_id": body.active_smtp_credential_id,
        }
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="organization.settings.updated",
        resource={"organization_id": str(organization.id), "by": principal.subject},
        principal=principal,
    )
    await db.commit()
    return _organization_to_dto(organization)


@router.get(
    "/settings/hire-approval",
    response_model=HireApprovalSetting,
    dependencies=[Depends(require_permission(perm(SETTINGS, VIEW)))],
)
async def get_hire_approval(db: DbSession, principal: CurrentPrincipal) -> HireApprovalSetting:
    enabled = await require_hire_approval(db, tenant_id=principal.tenant_id)
    return HireApprovalSetting(enabled=enabled)


@router.put(
    "/settings/hire-approval",
    response_model=HireApprovalSetting,
    dependencies=[Depends(require_permission(perm(SETTINGS, MANAGE)))],
)
async def put_hire_approval(
    body: HireApprovalSetting,
    db: DbSession,
    principal: CurrentPrincipal,
) -> HireApprovalSetting:
    await set_require_hire_approval(db, tenant_id=principal.tenant_id, enabled=body.enabled)
    await db.commit()
    return HireApprovalSetting(enabled=body.enabled)


@router.get(
    "/settings/copilot",
    response_model=CopilotSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, VIEW)))],
)
async def get_copilot_settings(db: DbSession, principal: CurrentPrincipal) -> CopilotSettings:
    return CopilotSettings(
        max_active_followups=await max_active_followups(db, tenant_id=principal.tenant_id)
    )


@router.put(
    "/settings/copilot",
    response_model=CopilotSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, MANAGE)))],
)
async def put_copilot_settings(
    body: CopilotSettings, db: DbSession, principal: CurrentPrincipal
) -> CopilotSettings:
    organization = await db.get(m.Organization, principal.tenant_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    organization.settings = {
        **organization.settings,
        "copilot_max_active_followups": body.max_active_followups,
    }
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="organization.settings.updated",
        resource={"organization_id": str(organization.id), "by": principal.subject},
        principal=principal,
    )
    await db.commit()
    return body
