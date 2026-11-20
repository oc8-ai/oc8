"""The shared TOTP-gate decision for `password_login` (Community's local
password auth, the only login entry point this edition ships). This module
only DECIDES, given an already-resolved member, what happens next -- it does
not mint, does not read a token, and does not know which route called it.
Kept as its own module (rather than inlined into `password_login`) so a
second entry point could reuse the identical decision without duplicating
the grace-period comparison, which is exactly the kind of thing that drifts
by a day, or by a boundary, without any test noticing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth.principal import Principal
from oc8.auth.two_factor_policy import (
    DEFAULT_POLICY,
    enrollable_methods,
    enrolled_methods,
    is_org_admin,
    load_policy,
    requires_member,
)

#: The grace period an organisation gets when its policy names none. The
#: policy's own `grace_days` is what the gate reads; this stays the one name
#: for the default so the two cannot drift.
TOTP_GRACE_DAYS = DEFAULT_POLICY.grace_days


@dataclass(frozen=True)
class TotpGateOutcome:
    """Exactly one of the three outcomes, plus the nag deadline.

    `full_session_ok` and the two `requires_*` flags are mutually exclusive
    by construction (every `return` below sets exactly one shape), so a
    caller may branch on them in any order.
    """

    #: True if the caller may proceed with a full session unchanged.
    full_session_ok: bool
    #: True if a TOTP credential is already enrolled: the caller owes us a
    #: code before any session at all (outcome 3).
    requires_totp_code: bool = False
    #: True if a mandatory member's grace period has run out with nothing
    #: enrolled: the caller owes us an enrollment (outcome 2).
    requires_totp_enrollment: bool = False
    #: Non-None only alongside `full_session_ok` for a member whose grace
    #: clock is running: the deadline, for the frontend's nag banner.
    totp_grace_expires_at: dt.datetime | None = None
    #: With `requires_totp_code`: the allowed methods the member enrolled and
    #: may answer with. With `requires_totp_enrollment`: the methods they may
    #: enrol in. Empty on a full session.
    methods: tuple[str, ...] = ()


async def member_id_for_principal(db: AsyncSession, principal: Principal) -> uuid.UUID | None:
    """The caller's own member row id, or None if they have none yet.

    Lives beside `totp_gate` for the same reason `totp_gate` itself does:
    a shared query is one place to get the `deleted_at IS NULL` term right,
    rather than a chance for a second copy to lose it.

    None rather than a 404 is deliberate: a member row that does not exist
    can carry neither a `TotpCredential` nor a `totp_grace_started_at`, so
    the gate's answer would be `full_session_ok` anyway. `GET /me` is what
    mints the row, and nothing orders it against a brand-new operator's
    first request.
    """
    return (
        await db.execute(
            select(m.OrgMember.id).where(
                m.OrgMember.tenant_id == principal.tenant_id,
                m.OrgMember.subject == principal.subject,
                m.OrgMember.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def second_factor_gate(db: AsyncSession, *, member_id: uuid.UUID) -> TotpGateOutcome:
    """The three-outcome decision, driven by the organisation's policy.

    `off` asks nobody, whatever they enrolled; enrolments are kept, so turning
    the policy back on asks the same members again at once.

    Otherwise enrolment is checked FIRST: a member holding an allowed method
    owes a code whatever their role and whatever their grace clock says, so
    a stale clock can never demand a fresh enrolment from somebody who has
    one. A method the policy no longer allows does not count: that member is
    treated as not enrolled.

    Only then is the requirement read. `admins` is the default and today's
    behaviour: an org_admin whose clock was started (by `assign_role` or
    `password_setup`) must enrol before it runs out. An org_admin who has no
    clock but enrolled a method the policy has since removed falls under it
    too, and their clock starts now. An admin with neither stays exempt, as
    the demo seed's admin always has been. `everyone` starts the clock at the
    member's first login under it.

    This writes the grace clock when it starts one; the caller's session
    commits it.
    """
    member = await db.get(m.OrgMember, member_id)
    if member is None:
        return TotpGateOutcome(full_session_ok=True)
    policy = await load_policy(db, member.tenant_id)
    if policy.requirement == "off":
        return TotpGateOutcome(full_session_ok=True)

    enrolled = await enrolled_methods(db, member_id)
    allowed = tuple(x for x in enrolled if x in policy.methods)
    if allowed:
        return TotpGateOutcome(full_session_ok=False, requires_totp_code=True, methods=allowed)
    if not requires_member(policy, admin=await is_org_admin(db, member)):
        return TotpGateOutcome(full_session_ok=True)
    if policy.requirement == "admins" and member.totp_grace_started_at is None and not enrolled:
        return TotpGateOutcome(full_session_ok=True)

    now = dt.datetime.now(tz=dt.UTC)
    if member.totp_grace_started_at is None:
        member.totp_grace_started_at = now
        await db.flush()
    grace_deadline = member.totp_grace_started_at + dt.timedelta(days=policy.grace_days)
    if now >= grace_deadline:
        return TotpGateOutcome(
            full_session_ok=False,
            requires_totp_enrollment=True,
            methods=tuple(await enrollable_methods(db, member.tenant_id, policy)),
        )
    return TotpGateOutcome(full_session_ok=True, totp_grace_expires_at=grace_deadline)
