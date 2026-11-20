"""Removing a member's second factors, and the audit events every two-factor
action leaves.

Shared by the member's own removal (`api/v1/two_factor.py`), an
administrator's reset (`api/v1/members.py`) and the enrol/verify routes, so a
removal always takes the vault secret and the backup codes with it, and every
event carries the same two facts: the member and the method. Never a code,
a secret or a hash.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.auth.principal import Principal
from oc8.auth.two_factor_policy import enrolled_methods, is_org_admin
from oc8.secrets.service import SecretNotFound, delete_secret

#: The existing audit category for actions on a member's own account
#: (password changes, sign-in address changes).
CATEGORY = "member"


async def audit(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    member_id: uuid.UUID,
    method: str | None = None,
    principal: Principal | None = None,
) -> None:
    resource: dict[str, object] = {"member_id": str(member_id)}
    if method is not None:
        resource["method"] = method
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="operator",
        actor_id=actor_id,
        category=CATEGORY,
        action=action,
        resource=resource,
        principal=principal,
    )


async def _drop_totp_secret(db: AsyncSession, tenant_id: uuid.UUID, cred: m.TotpCredential) -> None:
    if cred.secret_ref:
        # Already gone is the state we want.
        with contextlib.suppress(SecretNotFound):
            await delete_secret(db, tenant_id=tenant_id, ref=cred.secret_ref)


async def remove_method(
    db: AsyncSession, *, tenant_id: uuid.UUID, member: m.OrgMember, method: str
) -> None:
    """Remove one method. Once none is left the backup codes go too: they
    are a second factor of their own and must not outlive every method.

    An org_admin left with nothing gets a running grace clock, so a later
    `admins` policy asks them to enrol again rather than exempting them.
    """
    cred = (
        await db.execute(
            select(m.TotpCredential)
            .where(m.TotpCredential.member_id == member.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if method == "totp":
        if cred is not None:
            await _drop_totp_secret(db, tenant_id, cred)
            cred.secret_ref = ""
            cred.enrolled_at = None
    else:
        await db.execute(
            delete(m.SecondFactor).where(
                m.SecondFactor.member_id == member.id, m.SecondFactor.method == method
            )
        )
    await db.flush()
    if not await enrolled_methods(db, member.id):
        if cred is not None:
            await db.delete(cred)
        if member.totp_grace_started_at is None and await is_org_admin(db, member):
            member.totp_grace_started_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()


async def remove_all(db: AsyncSession, *, tenant_id: uuid.UUID, member: m.OrgMember) -> None:
    """An administrator's reset: every method, every backup code, every
    outstanding mailed code, and the grace clock restarted."""
    cred = (
        await db.execute(select(m.TotpCredential).where(m.TotpCredential.member_id == member.id))
    ).scalar_one_or_none()
    if cred is not None:
        await _drop_totp_secret(db, tenant_id, cred)
        await db.delete(cred)
    await db.execute(delete(m.SecondFactor).where(m.SecondFactor.member_id == member.id))
    now = dt.datetime.now(tz=dt.UTC)
    await db.execute(
        update(m.EmailOtpChallenge)
        .where(m.EmailOtpChallenge.member_id == member.id, m.EmailOtpChallenge.used_at.is_(None))
        .values(used_at=now)
    )
    member.totp_grace_started_at = now
    await db.flush()
