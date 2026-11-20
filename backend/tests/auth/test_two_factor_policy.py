"""The organisation's two-factor policy and the one gate that reads it.

The matrix below is the spec's own: every `requirement` against an enrolled
member, one with no grace clock, one inside the grace period and one past it,
for an admin and for somebody who is not. The gate is called directly -- it is
the decision; `password_login` only mints what it decides.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.auth.totp_gate import second_factor_gate
from oc8.auth.two_factor_policy import DEFAULT_POLICY, TwoFactorPolicy, policy_for
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


# --- policy_for -------------------------------------------------------------


def _org(settings: dict[str, Any]) -> m.Organization:
    return m.Organization(id=uuid.uuid4(), slug="x", name="x", settings=settings)


def test_no_stored_policy_is_todays_behaviour() -> None:
    assert policy_for(_org({})) == TwoFactorPolicy(
        requirement="admins", methods=("totp", "email", "passkey"), grace_days=7
    )
    assert policy_for(None) == DEFAULT_POLICY


def test_a_stored_policy_is_read_back() -> None:
    policy = policy_for(
        _org({"two_factor": {"requirement": "everyone", "methods": ["email"], "grace_days": 3}})
    )
    assert policy == TwoFactorPolicy(requirement="everyone", methods=("email",), grace_days=3)


@pytest.mark.parametrize(
    "stored",
    [
        "garbage",
        ["admins"],
        {"requirement": "sometimes"},
        {"methods": "totp"},
        {"methods": ["sms", 7]},
        {"grace_days": "seven"},
        {"grace_days": -1},
        {"grace_days": 999},
        {"grace_days": True},
    ],
)
def test_a_malformed_policy_falls_back_field_by_field(stored: object) -> None:
    policy = policy_for(_org({"two_factor": stored}))
    assert policy.requirement in {"off", "optional", "admins", "everyone"}
    assert set(policy.methods) <= {"totp", "email", "passkey"}
    assert 0 <= policy.grace_days <= 30


def test_unknown_methods_are_dropped_but_known_ones_kept() -> None:
    policy = policy_for(_org({"two_factor": {"methods": ["sms", "totp"]}}))
    assert policy.methods == ("totp",)


# --- the gate matrix --------------------------------------------------------

_NOW = dt.datetime.now(tz=dt.UTC)
#: Grace state -> `totp_grace_started_at`.
_CLOCKS: dict[str, dt.datetime | None] = {
    "enrolled": None,
    "no_clock": None,
    "in_grace": _NOW - dt.timedelta(days=1),
    "grace_over": _NOW - dt.timedelta(days=30),
}


async def _seed(
    app_session: AppSessionFactory,
    *,
    policy: dict[str, Any] | None,
    admin: bool,
    state: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        settings: dict[str, Any] = {} if policy is None else {"two_factor": policy}
        db.add(m.Organization(id=tenant, slug=f"o-{tenant.hex[:8]}", name="O", settings=settings))
        role = m.Role(tenant_id=tenant, name="org_admin" if admin else "Staff", builtin=admin)
        db.add(role)
        await db.flush()
        member = m.OrgMember(
            tenant_id=tenant,
            subject=f"{tenant.hex[:8]}@example.com",
            subject_uuid=uuid.uuid4(),
            email=f"{tenant.hex[:8]}@example.com",
            role_id=role.id,
            totp_grace_started_at=_CLOCKS[state],
        )
        db.add(member)
        await db.flush()
        if state == "enrolled":
            db.add(
                m.TotpCredential(
                    tenant_id=tenant,
                    member_id=member.id,
                    secret_ref=f"totp:{member.id}",
                    enrolled_at=_NOW,
                    backup_codes=[],
                )
            )
        return tenant, member.id


def _shape(outcome: Any) -> str:
    if outcome.requires_totp_code:
        return "code"
    if outcome.requires_totp_enrollment:
        return "enrol"
    return "nag" if outcome.totp_grace_expires_at is not None else "full"


#: (requirement, admin, state) -> expected outcome.
_MATRIX: list[tuple[str, bool, str, str]] = [
    # off: nobody is asked, enrolled or not.
    *[("off", a, s, "full") for a in (True, False) for s in _CLOCKS],
    # optional: only members who enrolled are asked.
    *[("optional", a, "enrolled", "code") for a in (True, False)],
    *[("optional", a, s, "full") for a in (True, False) for s in _CLOCKS if s != "enrolled"],
    # admins: an admin enrols within grace; a non-admin is only asked if enrolled.
    ("admins", True, "enrolled", "code"),
    ("admins", True, "no_clock", "full"),
    ("admins", True, "in_grace", "nag"),
    ("admins", True, "grace_over", "enrol"),
    ("admins", False, "enrolled", "code"),
    ("admins", False, "no_clock", "full"),
    ("admins", False, "in_grace", "full"),
    ("admins", False, "grace_over", "full"),
    # everyone: the clock starts at first login.
    *[("everyone", a, "enrolled", "code") for a in (True, False)],
    *[("everyone", a, "no_clock", "nag") for a in (True, False)],
    *[("everyone", a, "in_grace", "nag") for a in (True, False)],
    *[("everyone", a, "grace_over", "enrol") for a in (True, False)],
]


@pytest.mark.parametrize(("requirement", "admin", "state", "expected"), _MATRIX)
async def test_policy_matrix(
    app_session: AppSessionFactory, requirement: str, admin: bool, state: str, expected: str
) -> None:
    policy = {"requirement": requirement, "methods": ["totp", "email", "passkey"], "grace_days": 7}
    tenant, member_id = await _seed(app_session, policy=policy, admin=admin, state=state)
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == expected


@pytest.mark.parametrize(
    ("admin", "state", "expected"),
    [
        (True, "enrolled", "code"),
        (True, "no_clock", "full"),
        (True, "in_grace", "nag"),
        (True, "grace_over", "enrol"),
        (False, "enrolled", "code"),
        (False, "no_clock", "full"),
    ],
)
async def test_no_stored_policy_matches_admins_default(
    app_session: AppSessionFactory, admin: bool, state: str, expected: str
) -> None:
    tenant, member_id = await _seed(app_session, policy=None, admin=admin, state=state)
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == expected


async def test_the_challenge_names_the_methods_the_member_may_answer_with(
    app_session: AppSessionFactory,
) -> None:
    tenant, member_id = await _seed(app_session, policy=None, admin=False, state="enrolled")
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert outcome.methods == ("totp",)


async def test_everyone_starts_the_clock_once(app_session: AppSessionFactory) -> None:
    policy = {"requirement": "everyone", "methods": ["totp"], "grace_days": 7}
    tenant, member_id = await _seed(app_session, policy=policy, admin=False, state="no_clock")
    async with app_session(tenant) as db:
        first = await second_factor_gate(db, member_id=member_id)
    async with app_session(tenant) as db:
        second = await second_factor_gate(db, member_id=member_id)
        member = await db.get(m.OrgMember, member_id)
        assert member is not None and member.totp_grace_started_at is not None
    assert first.totp_grace_expires_at == second.totp_grace_expires_at


async def test_grace_days_come_from_the_policy(app_session: AppSessionFactory) -> None:
    policy = {"requirement": "everyone", "methods": ["totp"], "grace_days": 0}
    tenant, member_id = await _seed(app_session, policy=policy, admin=False, state="no_clock")
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == "enrol"
    assert outcome.methods == ("totp",)


# --- review focus 2: a method removed from the policy -----------------------


async def test_a_method_removed_from_the_policy_no_longer_counts_as_enrolled(
    app_session: AppSessionFactory,
) -> None:
    """An admin who enrolled TOTP (clock cleared at confirm) is, once TOTP is
    no longer allowed, treated as not enrolled: the clock restarts and they
    are not challenged with the method they may not use."""
    policy = {"requirement": "admins", "methods": ["email"], "grace_days": 7}
    tenant, member_id = await _seed(app_session, policy=policy, admin=True, state="enrolled")
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
        member = await db.get(m.OrgMember, member_id)
        assert member is not None and member.totp_grace_started_at is not None
    assert _shape(outcome) == "nag"


async def test_a_member_left_without_a_method_must_enrol_once_grace_is_over(
    app_session: AppSessionFactory,
) -> None:
    policy = {"requirement": "everyone", "methods": ["email"], "grace_days": 7}
    tenant, member_id = await _seed(app_session, policy=policy, admin=False, state="enrolled")
    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        assert member is not None
        member.totp_grace_started_at = _NOW - dt.timedelta(days=30)
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == "enrol"


async def test_optional_ignores_an_enrolment_of_a_removed_method(
    app_session: AppSessionFactory,
) -> None:
    policy = {"requirement": "optional", "methods": ["email"], "grace_days": 7}
    tenant, member_id = await _seed(app_session, policy=policy, admin=False, state="enrolled")
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == "full"


async def test_a_passkey_only_policy_offers_a_passkey_to_enrol(
    app_session: AppSessionFactory,
) -> None:
    policy = {"requirement": "everyone", "methods": ["passkey"], "grace_days": 0}
    tenant, member_id = await _seed(app_session, policy=policy, admin=False, state="enrolled")
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == "enrol"
    assert outcome.methods == ("passkey",)


async def test_passkeys_are_never_offered_without_a_usable_public_url(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "frontend_base_url", "http://oc8.example.com")
    policy = {"requirement": "everyone", "methods": ["totp", "passkey"], "grace_days": 0}
    tenant, member_id = await _seed(app_session, policy=policy, admin=False, state="grace_over")
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == "enrol"
    assert outcome.methods == ("totp",)


async def test_a_held_passkey_does_not_count_without_a_usable_public_url(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config

    policy = {"requirement": "everyone", "methods": ["totp", "passkey"], "grace_days": 0}
    tenant, member_id = await _seed(app_session, policy=policy, admin=False, state="grace_over")
    async with app_session(tenant) as db:
        db.add(
            m.SecondFactor(
                tenant_id=tenant,
                member_id=member_id,
                method="passkey",
                label="Laptop",
                data={"credential_id": "abc", "public_key": "def", "sign_count": 0},
            )
        )
    async with app_session(tenant) as db:
        assert _shape(await second_factor_gate(db, member_id=member_id)) == "code"
    monkeypatch.setattr(config.get_settings(), "frontend_base_url", "")
    async with app_session(tenant) as db:
        outcome = await second_factor_gate(db, member_id=member_id)
    assert _shape(outcome) == "enrol"
    assert "passkey" not in outcome.methods


# --- review focus 5: off keeps enrolments, on asks again at once ------------


async def test_off_then_on_asks_an_enrolled_member_again_at_once(
    app_session: AppSessionFactory,
) -> None:
    off = {"requirement": "off", "methods": ["totp"], "grace_days": 7}
    tenant, member_id = await _seed(app_session, policy=off, admin=True, state="enrolled")
    async with app_session(tenant) as db:
        assert _shape(await second_factor_gate(db, member_id=member_id)) == "full"
        cred_count = len((await db.execute(m.TotpCredential.__table__.select())).fetchall())
        assert cred_count == 1
    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        assert org is not None
        org.settings = {"two_factor": {**off, "requirement": "admins"}}
    async with app_session(tenant) as db:
        assert _shape(await second_factor_gate(db, member_id=member_id)) == "code"
