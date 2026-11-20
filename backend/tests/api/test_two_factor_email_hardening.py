"""Security review follow-ups for the email method: a login code goes only to
the address that was confirmed, guessing is capped per member over a day, and
a changed sign-in address never leaves a required member exempt."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import Principal, get_identity_provider
from oc8.auth.email_otp import DAILY_FAILED_ATTEMPTS
from oc8.auth.totp_gate import second_factor_gate
from oc8.authz.permissions import MEMBER_ROLE
from tests.api import test_two_factor_email as _email
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

Outbox = _email.Outbox
outbox = _email.outbox
_kek = _email._kek

_EVERYONE = {"requirement": "everyone", "methods": ["totp", "email"], "grace_days": 7}


async def _set_factor_address(
    app_session: AppSessionFactory, s: _email.Seeded, address: str | None
) -> None:
    async with app_session(s.tenant) as db:
        factor = (await db.execute(select(m.SecondFactor))).scalar_one()
        factor.data = {} if address is None else {"address": address}


async def _actions(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.AuditEvent]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action.like("two_factor.%"))
                )
            ).scalars()
        )


# --- 1: the confirmed address ---------------------------------------------------


async def test_enrolment_stores_the_confirmed_address(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _email._seed(app_session)
    async with _email._http() as http:
        await _email._enrol(http, s, outbox)
    async with app_session(s.tenant) as db:
        factor = (await db.execute(select(m.SecondFactor))).scalar_one()
    assert factor.data == {"address": s.subject}


async def test_a_login_code_goes_to_the_confirmed_address(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _email._seed(app_session, email_enrolled=True)
    await _set_factor_address(app_session, s, s.subject)
    async with _email._http() as http:
        r = await http.post(_email._SEND, headers=s.token("totp:challenge"))
    assert r.status_code == 200, r.text
    assert outbox.sent[-1]["to"] == s.subject


@pytest.mark.parametrize("stored", [None, "someone-else@example.com"])
async def test_no_login_code_without_a_matching_confirmed_address(
    app_session: AppSessionFactory, outbox: Outbox, stored: str | None
) -> None:
    """A factor with no confirmed address, or one confirmed for an address the
    member no longer signs in with, cannot answer a login."""
    s = await _email._seed(app_session, email_enrolled=True)
    await _set_factor_address(app_session, s, stored)
    async with _email._http() as http:
        r = await http.post(_email._SEND, headers=s.token("totp:challenge"))
    assert r.status_code == 403, r.text
    assert outbox.sent == []
    async with app_session(s.tenant) as db:
        outcome = await second_factor_gate(db, member_id=s.member_id)
    assert "email" not in outcome.methods


async def test_an_address_change_from_the_token_drops_the_email_factor(
    app_session: AppSessionFactory,
) -> None:
    """The per-request member sync (`scope_for_principal`) copies a token's
    email claim onto the row; that is an address change like any other."""
    from oc8.authz.scope import scope_for_principal

    s = await _email._seed(app_session, email_enrolled=True)
    await _set_factor_address(app_session, s, s.subject)
    principal = Principal(
        subject=s.subject,
        tenant_id=s.tenant,
        role=MEMBER_ROLE,
        kind="operator",
        scopes=[],
        email="new@example.com",
    )
    async with app_session(s.tenant) as db:
        await scope_for_principal(db, principal, upsert=True)
    async with app_session(s.tenant) as db:
        assert (await db.execute(select(m.SecondFactor))).scalars().all() == []


async def test_an_address_change_through_member_upsert_drops_the_email_factor(
    app_session: AppSessionFactory,
) -> None:
    from oc8.workspace.members import upsert_member

    s = await _email._seed(app_session, email_enrolled=True)
    await _set_factor_address(app_session, s, s.subject)
    async with app_session(s.tenant) as db:
        await upsert_member(db, tenant_id=s.tenant, subject=s.subject, email="other@example.com")
    async with app_session(s.tenant) as db:
        assert (await db.execute(select(m.SecondFactor))).scalars().all() == []


# --- 2: a daily cap on wrong codes ----------------------------------------------


async def _spent_attempts(app_session: AppSessionFactory, s: _email.Seeded, total: int) -> None:
    async with app_session(s.tenant) as db:
        db.add(
            m.EmailOtpChallenge(
                tenant_id=s.tenant,
                member_id=s.member_id,
                purpose="login",
                code_hash="x",
                expires_at=dt.datetime.now(tz=dt.UTC),
                attempts=total,
                used_at=dt.datetime.now(tz=dt.UTC),
            )
        )


async def test_the_daily_cap_refuses_even_the_right_code_and_is_audited_once(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _email._seed(app_session, email_enrolled=True)
    await _set_factor_address(app_session, s, s.subject)
    await _spent_attempts(app_session, s, DAILY_FAILED_ATTEMPTS - 1)
    challenge = s.token("totp:challenge")
    async with _email._http() as http:
        assert (await http.post(_email._SEND, headers=challenge)).status_code == 200
        code = outbox.last_code()
        wrong = "000000" if code != "000000" else "111111"
        r = await http.post(_email._VERIFY, json={"code": wrong}, headers=challenge)
        assert r.status_code == 401
        r = await http.post(_email._VERIFY, json={"code": code}, headers=challenge)
        assert r.status_code == 429, r.text
        assert "token" not in r.json()
        r = await http.post(_email._VERIFY, json={"code": code}, headers=challenge)
        assert r.status_code == 429
    locked = [e for e in await _actions(app_session, s.tenant) if e.action == "two_factor.locked"]
    assert len(locked) == 1


async def test_the_daily_cap_refuses_a_new_code(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _email._seed(app_session, email_enrolled=True)
    await _set_factor_address(app_session, s, s.subject)
    await _spent_attempts(app_session, s, DAILY_FAILED_ATTEMPTS)
    async with _email._http() as http:
        r = await http.post(_email._SEND, headers=s.token("totp:challenge"))
    assert r.status_code == 429, r.text
    assert "backup code" in r.json()["detail"].lower()
    assert outbox.sent == []


async def test_attempts_older_than_a_day_do_not_count(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _email._seed(app_session, email_enrolled=True)
    await _set_factor_address(app_session, s, s.subject)
    await _spent_attempts(app_session, s, DAILY_FAILED_ATTEMPTS)
    await _email._age_challenges(
        app_session, s, created_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=25)
    )
    async with _email._http() as http:
        r = await http.post(_email._SEND, headers=s.token("totp:challenge"))
    assert r.status_code == 200, r.text


# --- 3: a changed address never leaves a required member exempt -------------------


async def test_a_required_member_whose_address_changed_must_enrol_at_next_login(
    app_session: AppSessionFactory,
) -> None:
    s = await _email._seed(app_session, email_enrolled=True, policy=_EVERYONE)
    await _set_factor_address(app_session, s, s.subject)
    async with app_session(s.tenant) as db:
        member = await db.get(m.OrgMember, s.member_id)
        assert member is not None
        member.totp_grace_started_at = None
    admin = get_identity_provider().mint(tenant_id=s.tenant, subject="boss", role="org_admin")
    async with _email._http() as http:
        r = await http.put(
            f"/api/v1/members/{s.member_id}/subject",
            json={"subject": f"new-{s.subject}"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert r.status_code == 200, r.text
    async with app_session(s.tenant) as db:
        outcome = await second_factor_gate(db, member_id=s.member_id)
    assert outcome.requires_totp_enrollment
    removed = [e for e in await _actions(app_session, s.tenant) if e.action == "two_factor.removed"]
    assert len(removed) == 1
    assert removed[0].resource == {"member_id": str(s.member_id), "method": "email"}
    assert removed[0].reason == "email_changed"


async def test_a_member_with_another_method_keeps_their_grace_untouched(
    app_session: AppSessionFactory,
) -> None:
    s = await _email._seed(app_session, email_enrolled=True, policy=_EVERYONE)
    await _set_factor_address(app_session, s, s.subject)
    async with app_session(s.tenant) as db:
        db.add(
            m.TotpCredential(
                tenant_id=s.tenant,
                member_id=s.member_id,
                secret_ref="x",
                enrolled_at=dt.datetime.now(tz=dt.UTC),
                backup_codes=[],
            )
        )
        member = await db.get(m.OrgMember, s.member_id)
        assert member is not None
        member.totp_grace_started_at = None
    admin = get_identity_provider().mint(tenant_id=s.tenant, subject="boss", role="org_admin")
    async with _email._http() as http:
        r = await http.put(
            f"/api/v1/members/{s.member_id}/subject",
            json={"subject": f"new-{s.subject}"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert r.status_code == 200, r.text
    async with app_session(s.tenant) as db:
        member = await db.get(m.OrgMember, s.member_id)
        assert member is not None and member.totp_grace_started_at is None
        outcome = await second_factor_gate(db, member_id=s.member_id)
    assert outcome.requires_totp_code and outcome.methods == ("totp",)


# --- removal race: the member row is locked ---------------------------------------


async def test_removal_locks_the_member_row_before_counting(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    s = await _email._seed(app_session, email_enrolled=True, policy=_EVERYONE)
    statements: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _capture)
    try:
        async with _email._http() as http:
            await http.request(
                "DELETE",
                "/api/v1/auth/two-factor/methods/email",
                json={"currentPassword": "Whatever123!"},
                headers=s.token(),
            )
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)
    locks = [i for i, x in enumerate(statements) if "FROM org_member" in x and "FOR UPDATE" in x]
    reads = [i for i, x in enumerate(statements) if "FROM second_factor" in x]
    assert locks, "the member row is never locked"
    assert reads and locks[0] < reads[0]


async def test_two_concurrent_removals_leave_a_required_member_one_method(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    import asyncio

    import pyotp

    s = await _email._seed(app_session, email_enrolled=True, policy=_EVERYONE)
    async with _email._http() as http:
        secret = (await http.post("/api/v1/auth/totp/enroll", headers=s.token())).json()["secret"]
        r = await http.post(
            "/api/v1/auth/totp/confirm",
            json={"secret": secret, "code": pyotp.TOTP(secret).now()},
            headers=s.token(),
        )
        assert r.status_code == 200, r.text
        results = await asyncio.gather(
            *(
                http.request(
                    "DELETE",
                    f"/api/v1/auth/two-factor/methods/{method}",
                    json={"currentPassword": "Whatever123!"},
                    headers=s.token(),
                )
                for method in ("totp", "email")
            )
        )
    assert sorted(r.status_code for r in results) == [204, 409]
    async with app_session(s.tenant) as db:
        outcome = await second_factor_gate(db, member_id=s.member_id)
    assert outcome.requires_totp_code
