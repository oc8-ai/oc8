"""Two routes that end the gamified onboarding wizard's /welcome redirect
(design: gamified-onboarding-wizard-design.md). Deliberately two distinct
routes rather than one taking a status parameter -- the same "no parameter
here to get wrong" reasoning as useClarifications in hooks.ts, and it keeps a
client from ever being able to set onboarding_status back to 'pending'."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission, unguarded
from oc8.authz.permissions import DEPARTMENT, MANAGE, perm
from oc8.authz.scope import scope_for_principal
from oc8.schemas.base import CamelModel

router = APIRouter()


class OnboardingStatusDTO(CamelModel):
    onboarding_status: str


async def _set_status(db: DbSession, tenant_id: uuid.UUID, status: str) -> OnboardingStatusDTO:
    org = await db.get(m.Organization, tenant_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    org.onboarding_status = status
    await db.flush()
    return OnboardingStatusDTO(onboarding_status=status)


@router.post(
    "/onboarding/complete",
    response_model=OnboardingStatusDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def complete_onboarding(db: DbSession, principal: CurrentPrincipal) -> OnboardingStatusDTO:
    return await _set_status(db, principal.tenant_id, "completed")


@router.post(
    "/onboarding/skip",
    response_model=OnboardingStatusDTO,
    dependencies=[Depends(require_permission(perm(DEPARTMENT, MANAGE)))],
)
async def skip_onboarding(db: DbSession, principal: CurrentPrincipal) -> OnboardingStatusDTO:
    return await _set_status(db, principal.tenant_id, "skipped")


class MemberOnboardingStatusDTO(CamelModel):
    member_onboarding_status: str


async def _set_member_status(
    db: DbSession, principal: CurrentPrincipal, value: str
) -> MemberOnboardingStatusDTO:
    """The caller's own row only: there is no member id to pass, so nobody can
    end somebody else's welcome."""
    try:
        member, _scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="this token does not belong to a person with an account"
        ) from exc
    if member is None:
        raise HTTPException(status_code=404, detail="no member record for this caller")
    member.onboarding_status = value
    await db.flush()
    return MemberOnboardingStatusDTO(member_onboarding_status=value)


@router.post(
    "/onboarding/member/complete",
    response_model=MemberOnboardingStatusDTO,
    dependencies=[Depends(unguarded("self-service: sets only the caller's own member row"))],
)
async def complete_member_onboarding(
    db: DbSession, principal: CurrentPrincipal
) -> MemberOnboardingStatusDTO:
    return await _set_member_status(db, principal, "completed")


@router.post(
    "/onboarding/member/skip",
    response_model=MemberOnboardingStatusDTO,
    dependencies=[Depends(unguarded("self-service: sets only the caller's own member row"))],
)
async def skip_member_onboarding(
    db: DbSession, principal: CurrentPrincipal
) -> MemberOnboardingStatusDTO:
    return await _set_member_status(db, principal, "skipped")
