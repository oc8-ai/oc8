"""Tenant settings for the signed-in organization and approval policies."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from oc8 import models as m
from oc8.agents.hire import require_hire_approval, set_require_hire_approval
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.auth.totp_gate import member_id_for_principal
from oc8.auth.two_factor_policy import (
    MAX_GRACE_DAYS,
    METHODS,
    SETTINGS_KEY,
    TwoFactorPolicy,
    enrolled_methods,
    is_org_admin,
    policy_for,
    requires_member,
)
from oc8.authz.permissions import MANAGE, SETTINGS, VIEW, perm
from oc8.copilot.followups import max_active_followups
from oc8.credentials.service import list_credentials
from oc8.mail.send import active_smtp_credential
from oc8.schemas.base import CamelModel
from oc8.triggers.zones import valid_timezone

router = APIRouter()


class HireApprovalSetting(BaseModel):
    enabled: bool


class CopilotSettings(CamelModel):
    max_active_followups: int = Field(ge=1, le=100)


class TwoFactorSettingsUpdate(CamelModel):
    requirement: Literal["off", "optional", "admins", "everyone"]
    methods: list[Literal["totp", "email", "passkey"]]
    grace_days: int = Field(ge=0, le=MAX_GRACE_DAYS)


class TwoFactorSettings(TwoFactorSettingsUpdate):
    #: Whether outgoing mail is configured; `email` cannot be allowed without it.
    email_available: bool


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
    #: IANA zone a new agent schedule is read in unless it names another.
    timezone: str = "UTC"


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
    #: Omitted leaves the zone as it is (forms that post only name/region).
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value: str | None) -> str | None:
        return valid_timezone(value) if value is not None else None

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
        timezone=organization.timezone,
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
    resource: dict[str, object] = {"organization_id": str(organization.id), "by": principal.subject}
    if body.timezone is not None and body.timezone != organization.timezone:
        # New schedules move to it; recorded so a changed default is traceable.
        resource["timezone"] = {"from": organization.timezone, "to": body.timezone}
        organization.timezone = body.timezone
    if "active_smtp_credential_id" in body.model_fields_set:
        if body.active_smtp_credential_id is not None:
            # Server-side filter (credentials/service.py:108-115), so this
            # never walks every credential the tenant owns.
            smtp_credentials = await list_credentials(
                db,
                tenant_id=principal.tenant_id,
                credential_type="smtp_server",
                owner="organisation",
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
        resource=resource,
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


async def _two_factor_dto(
    db: DbSession, organization: m.Organization, policy: TwoFactorPolicy
) -> TwoFactorSettings:
    mail = await active_smtp_credential(db, tenant_id=organization.id) is not None
    return TwoFactorSettings(
        requirement=policy.requirement,
        methods=list(policy.methods),
        grace_days=policy.grace_days,
        email_available=mail,
    )


@router.get(
    "/settings/organization/two-factor",
    response_model=TwoFactorSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, VIEW)))],
)
async def get_two_factor_settings(db: DbSession, principal: CurrentPrincipal) -> TwoFactorSettings:
    organization = await db.get(m.Organization, principal.tenant_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return await _two_factor_dto(db, organization, policy_for(organization))


@router.put(
    "/settings/organization/two-factor",
    response_model=TwoFactorSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, MANAGE)))],
)
async def put_two_factor_settings(
    body: TwoFactorSettingsUpdate, db: DbSession, principal: CurrentPrincipal
) -> TwoFactorSettings:
    """Save the organisation's two-factor policy.

    Refused when it would leave nothing to sign in with: no method while 2FA
    is on, email without a mail server to send the code, or a policy that
    requires the saving admin to use a method they do not hold. The last one
    is what keeps a save from locking every admin out: whoever tightens the
    policy has already proven they can satisfy it.
    """
    organization = await db.get(m.Organization, principal.tenant_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    wanted = set(body.methods)
    new = TwoFactorPolicy(
        requirement=body.requirement,
        methods=tuple(x for x in METHODS if x in wanted),
        grace_days=body.grace_days,
    )
    if new.requirement != "off" and not new.methods:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Allow at least one method, or turn two-factor authentication off.",
        )
    if (
        "email" in new.methods
        and await active_smtp_credential(db, tenant_id=principal.tenant_id) is None
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A code by email needs outgoing mail. Configure a mail server first.",
        )
    member_id = await member_id_for_principal(db, principal)
    member = await db.get(m.OrgMember, member_id) if member_id is not None else None
    admin = member is not None and await is_org_admin(db, member)
    if requires_member(new, admin=admin):
        held = await enrolled_methods(db, member.id) if member is not None else []
        if not any(x in new.methods for x in held):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This policy would require you to use a second factor you have not set up. "
                "Set up one of the allowed methods first.",
            )

    old = policy_for(organization)
    organization.settings = {**organization.settings, SETTINGS_KEY: new.as_settings()}
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="organization.settings.updated",
        resource={
            "organization_id": str(organization.id),
            "by": principal.subject,
            "two_factor": {"from": old.as_settings(), "to": new.as_settings()},
        },
        principal=principal,
    )
    dto = await _two_factor_dto(db, organization, new)
    await db.commit()
    return dto
