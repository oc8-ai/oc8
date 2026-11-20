"""An organisation's two-factor policy, and which methods a member holds.

Stored in `organization.settings["two_factor"]` (two-factor methods and
policy design). An organisation that never saved one gets `DEFAULT_POLICY`,
which is exactly how login behaved before the policy existed: org admins must
enrol after seven days, anybody who enrolled is asked.

`policy_for` never raises. The JSON is written by one route that validates it,
but a hand-edited or half-migrated value must not turn every login into a 500;
each field falls back to its default on its own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.mail.send import active_smtp_credential

Requirement = Literal["off", "optional", "admins", "everyone"]

REQUIREMENTS: tuple[Requirement, ...] = ("off", "optional", "admins", "everyone")
#: Every method a policy may name, in the order the gate reports them.
#: `passkey` is accepted in a policy now, but nobody can hold one yet.
METHODS: tuple[str, ...] = ("totp", "email", "passkey")
#: Methods a member can enrol in today.
ENROLLABLE_METHODS: tuple[str, ...] = ("totp", "email")
MAX_GRACE_DAYS = 30
SETTINGS_KEY = "two_factor"
ORG_ADMIN_ROLE = "org_admin"


@dataclass(frozen=True)
class TwoFactorPolicy:
    requirement: Requirement
    methods: tuple[str, ...]
    grace_days: int

    def as_settings(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "methods": list(self.methods),
            "grace_days": self.grace_days,
        }


DEFAULT_POLICY = TwoFactorPolicy(requirement="admins", methods=METHODS, grace_days=7)


def normalise_methods(raw: object) -> tuple[str, ...] | None:
    """Known methods in canonical order, or None if `raw` is not a list."""
    if not isinstance(raw, list):
        return None
    wanted = {x for x in raw if isinstance(x, str)}
    return tuple(x for x in METHODS if x in wanted)


def policy_for(organization: m.Organization | None) -> TwoFactorPolicy:
    stored = (organization.settings or {}).get(SETTINGS_KEY) if organization is not None else None
    if not isinstance(stored, dict):
        return DEFAULT_POLICY
    requirement = stored.get("requirement")
    if requirement not in REQUIREMENTS:
        requirement = DEFAULT_POLICY.requirement
    methods = normalise_methods(stored.get("methods"))
    if methods is None:
        methods = DEFAULT_POLICY.methods
    grace_days = stored.get("grace_days")
    # `bool` is an `int` subclass; `true` is not a number of days.
    if (
        not isinstance(grace_days, int)
        or isinstance(grace_days, bool)
        or not 0 <= grace_days <= MAX_GRACE_DAYS
    ):
        grace_days = DEFAULT_POLICY.grace_days
    return TwoFactorPolicy(
        requirement=cast(Requirement, requirement), methods=methods, grace_days=grace_days
    )


async def load_policy(db: AsyncSession, tenant_id: uuid.UUID) -> TwoFactorPolicy:
    return policy_for(await db.get(m.Organization, tenant_id))


async def enrolled_methods(db: AsyncSession, member_id: uuid.UUID) -> list[str]:
    """Every method the member has enrolled, allowed by the policy or not."""
    found: set[str] = set()
    totp_enrolled_at = (
        await db.execute(
            select(m.TotpCredential.enrolled_at).where(m.TotpCredential.member_id == member_id)
        )
    ).scalar_one_or_none()
    if totp_enrolled_at is not None:
        found.add("totp")
    # Only methods a member can enrol today: passkey rows cannot exist yet,
    # and until passkey assertion ships one must never answer a challenge.
    found.update(
        (
            await db.execute(
                select(m.SecondFactor.method).where(
                    m.SecondFactor.member_id == member_id,
                    m.SecondFactor.method.in_(ENROLLABLE_METHODS),
                )
            )
        ).scalars()
    )
    return [x for x in METHODS if x in found]


async def member_methods(
    db: AsyncSession, member: m.OrgMember, policy: TwoFactorPolicy | None = None
) -> list[str]:
    """The member's enrolled methods that the policy allows: what they may
    answer a login challenge with."""
    if policy is None:
        policy = await load_policy(db, member.tenant_id)
    return [x for x in await enrolled_methods(db, member.id) if x in policy.methods]


async def enrollable_methods(
    db: AsyncSession, tenant_id: uuid.UUID, policy: TwoFactorPolicy
) -> list[str]:
    """Allowed methods a member can enrol in right now. Email needs a mail
    server to send the code through."""
    mail = await active_smtp_credential(db, tenant_id=tenant_id) is not None
    return [x for x in policy.methods if x in ENROLLABLE_METHODS and (x != "email" or mail)]


async def is_org_admin(db: AsyncSession, member: m.OrgMember) -> bool:
    """Whether the member's assigned role is `org_admin`.

    The assigned role and not the token's claim: `password_login` mints every
    password session with the empty member role, so the row is the only
    place admin-ness lives for the people this gate is about.
    """
    if member.role_id is None:
        return False
    name = (
        await db.execute(select(m.Role.name).where(m.Role.id == member.role_id))
    ).scalar_one_or_none()
    return name == ORG_ADMIN_ROLE


def requires_member(policy: TwoFactorPolicy, *, admin: bool) -> bool:
    """Whether the policy names this member's group at all."""
    if policy.requirement == "everyone":
        return True
    return policy.requirement == "admins" and admin
