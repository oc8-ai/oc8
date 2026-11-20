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
from oc8.auth.passkey import spend_challenges
from oc8.auth.principal import Principal
from oc8.auth.totp import generate_backup_codes, hash_backup_code
from oc8.auth.two_factor_policy import (
    enrolled_methods,
    is_org_admin,
    load_policy,
    member_methods,
    requires_member,
)
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
    reason: str | None = None,
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
        reason=reason,
        principal=principal,
    )


async def lock_member(db: AsyncSession, member_id: uuid.UUID) -> m.OrgMember | None:
    """`SELECT ... FOR UPDATE` on the member row.

    Every change to a member's set of second factors takes this lock first,
    so "is this the last method the policy requires?" and the removal happen
    against one state: two concurrent removals of two different methods would
    otherwise each see the other still present, and both succeed.
    """
    return (
        await db.execute(select(m.OrgMember).where(m.OrgMember.id == member_id).with_for_update())
    ).scalar_one_or_none()


async def sign_in_address_changed(
    db: AsyncSession,
    member: m.OrgMember,
    *,
    actor_id: uuid.UUID | None = None,
    principal: Principal | None = None,
) -> None:
    """The member's sign-in address changed: the email factor was confirmed
    for the old one and goes.

    If the policy requires a second factor from them and nothing allowed is
    left, the grace period is set to have run out already: their next login
    demands an enrolment instead of handing them a fresh week, or none at
    all. Fail closed, because the change may not have been theirs.
    """
    # Flush first: the caller's address change must be what the policy
    # check below sees, and nothing here may read the row from before it.
    await db.flush()
    if await lock_member(db, member.id) is None:
        return
    # An enrolment code mailed to the old address must not confirm the new one.
    await db.execute(
        update(m.EmailOtpChallenge)
        .where(m.EmailOtpChallenge.member_id == member.id, m.EmailOtpChallenge.used_at.is_(None))
        .values(used_at=dt.datetime.now(tz=dt.UTC))
    )
    removed = (
        await db.execute(
            delete(m.SecondFactor)
            .where(m.SecondFactor.member_id == member.id, m.SecondFactor.method == "email")
            .returning(m.SecondFactor.id)
        )
    ).first()
    if removed is None:
        return
    policy = await load_policy(db, member.tenant_id)
    required = policy.requirement != "off" and requires_member(
        policy, admin=await is_org_admin(db, member)
    )
    if required and not await member_methods(db, member, policy):
        member.totp_grace_started_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            days=policy.grace_days, seconds=1
        )
    await audit(
        db,
        tenant_id=member.tenant_id,
        actor_id=actor_id,
        action="two_factor.removed",
        member_id=member.id,
        method="email",
        principal=principal,
        reason="email_changed",
    )
    await db.flush()


async def _drop_totp_secret(db: AsyncSession, tenant_id: uuid.UUID, cred: m.TotpCredential) -> None:
    if cred.secret_ref:
        # Already gone is the state we want.
        with contextlib.suppress(SecretNotFound):
            await delete_secret(db, tenant_id=tenant_id, ref=cred.secret_ref)


async def issue_backup_codes_if_none(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> list[str]:
    """Backup codes for a member enrolling a method other than TOTP, when
    they hold none yet; empty otherwise. Shown once by the caller.

    Without a TOTP secret the `totp_credential` row only carries the codes.
    """
    cred = (
        await db.execute(
            select(m.TotpCredential)
            .where(m.TotpCredential.member_id == member_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cred is not None and cred.backup_codes:
        return []
    codes = generate_backup_codes()
    hashed: list[dict[str, str | None]] = [
        {"hash": hash_backup_code(c), "used_at": None} for c in codes
    ]
    if cred is None:
        db.add(
            m.TotpCredential(
                tenant_id=tenant_id,
                member_id=member_id,
                secret_ref="",
                enrolled_at=None,
                backup_codes=hashed,
            )
        )
    else:
        cred.backup_codes = hashed
    await db.flush()
    return codes


async def remove_method(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member: m.OrgMember,
    method: str,
    factor_id: uuid.UUID | None = None,
) -> None:
    """Remove one method, or with `factor_id` one passkey of several. Once
    none is left the backup codes go too: they are a second factor of their
    own and must not outlive every method.

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
        stmt = delete(m.SecondFactor).where(
            m.SecondFactor.member_id == member.id, m.SecondFactor.method == method
        )
        if factor_id is not None:
            stmt = stmt.where(m.SecondFactor.id == factor_id)
        await db.execute(stmt)
    await db.flush()
    if not await enrolled_methods(db, member.id):
        if cred is not None:
            await db.delete(cred)
        if member.totp_grace_started_at is None and await is_org_admin(db, member):
            member.totp_grace_started_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()


async def remove_all(db: AsyncSession, *, tenant_id: uuid.UUID, member: m.OrgMember) -> None:
    """An administrator's reset: every method, every backup code, every
    outstanding mailed code or passkey challenge, and the grace clock
    restarted."""
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
    await spend_challenges(db, member.id)
    member.totp_grace_started_at = now
    await db.flush()
