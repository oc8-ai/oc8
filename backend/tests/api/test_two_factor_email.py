"""A code by email as a second factor: enrolment, the login challenge, and
everything that must refuse a code (review focus 3) or a pending token
(review focus 4)."""

from __future__ import annotations

import base64
import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from oc8 import models as m
from oc8.api.deps import deny_totp_pending_principals
from oc8.api.v1 import two_factor
from oc8.auth import get_identity_provider
from oc8.auth.password import hash_password
from oc8.authz.permissions import MEMBER_ROLE
from oc8.mail.send import SmtpConfig
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_ENROLL = "/api/v1/auth/two-factor/email/enroll"
_CONFIRM = "/api/v1/auth/two-factor/email/confirm"
_SEND = "/api/v1/auth/two-factor/email/send"
_VERIFY = "/api/v1/auth/two-factor/email/verify"
_TOTP_VERIFY = "/api/v1/auth/totp/verify"
_TOTP_ENROLL = "/api/v1/auth/totp/enroll"
_TOTP_CONFIRM = "/api/v1/auth/totp/confirm"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@dataclass
class Outbox:
    sent: list[dict[str, str]] = field(default_factory=list)
    fail: bool = False

    def last_code(self) -> str:
        match = re.search(r"\b(\d{6})\b", self.sent[-1]["body"])
        assert match is not None
        return match.group(1)


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()

    async def _resolve(db: Any, *, tenant_id: uuid.UUID) -> SmtpConfig:
        return SmtpConfig(
            tenant_id=tenant_id,
            credential_id=uuid.uuid4(),
            host="smtp.example.com",
            port=587,
            username="",
            password="",
            use_tls=True,
            from_address="oc8@example.com",
        )

    async def _deliver(config: SmtpConfig, *, to: str, subject: str, body: str) -> bool:
        if box.fail:
            return False
        box.sent.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(two_factor, "resolve_smtp_config", _resolve)
    monkeypatch.setattr(two_factor, "deliver", _deliver)
    return box


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@dataclass
class Seeded:
    tenant: uuid.UUID
    member_id: uuid.UUID
    subject: str

    def token(self, *scopes: str) -> dict[str, str]:
        token = get_identity_provider().mint(
            tenant_id=self.tenant, subject=self.subject, role=MEMBER_ROLE, scopes=list(scopes)
        )
        return {"Authorization": f"Bearer {token}"}


async def _seed(
    app_session: AppSessionFactory,
    *,
    email_enrolled: bool = False,
    policy: dict[str, Any] | None = None,
) -> Seeded:
    tenant = uuid.uuid4()
    subject = f"m-{tenant.hex[:8]}@example.com"
    settings: dict[str, Any] = {"active_smtp_credential_id": str(uuid.uuid4())}
    if policy is not None:
        settings["two_factor"] = policy
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"o-{tenant.hex[:8]}", name="O", settings=settings))
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid4(),
            email=subject,
            password_hash=hash_password("Whatever123!"),
            totp_grace_started_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add(member)
        await db.flush()
        if email_enrolled:
            db.add(
                m.SecondFactor(
                    tenant_id=tenant,
                    member_id=member.id,
                    method="email",
                    data={"address": subject},
                )
            )
        return Seeded(tenant=tenant, member_id=member.id, subject=subject)


async def _enrol(http: AsyncClient, s: Seeded, outbox: Outbox) -> list[str]:
    r = await http.post(_ENROLL, headers=s.token())
    assert r.status_code == 200, r.text
    r = await http.post(_CONFIRM, json={"code": outbox.last_code()}, headers=s.token())
    assert r.status_code == 200, r.text
    codes: list[str] = r.json()["backupCodes"]
    return codes


async def _age_challenges(app_session: AppSessionFactory, s: Seeded, **values: Any) -> None:
    async with app_session(s.tenant) as db:
        await db.execute(
            update(m.EmailOtpChallenge)
            .where(m.EmailOtpChallenge.member_id == s.member_id)
            .values(**values)
        )


# --- enrolment ---------------------------------------------------------------


async def test_enrolment_mails_a_code_and_confirm_enrols_with_backup_codes(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        r = await http.post(_ENROLL, headers=s.token())
        assert r.status_code == 200, r.text
        assert r.json() == {"sentTo": s.subject}
        assert outbox.sent[-1]["to"] == s.subject
        assert "code" not in r.text.lower()
        r = await http.post(_CONFIRM, json={"code": outbox.last_code()}, headers=s.token())
        assert r.status_code == 200, r.text
        assert len(r.json()["backupCodes"]) == 10

    async with app_session(s.tenant) as db:
        factor = (await db.execute(select(m.SecondFactor))).scalar_one()
        assert factor.method == "email"
        member = await db.get(m.OrgMember, s.member_id)
        assert member is not None and member.totp_grace_started_at is None
        challenge = (await db.execute(select(m.EmailOtpChallenge))).scalar_one()
        assert challenge.used_at is not None
        assert outbox.last_code() not in challenge.code_hash


async def test_confirm_with_a_wrong_code_enrols_nothing(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        await http.post(_ENROLL, headers=s.token())
        wrong = "000000" if outbox.last_code() != "000000" else "111111"
        r = await http.post(_CONFIRM, json={"code": wrong}, headers=s.token())
        assert r.status_code == 401, r.text
    async with app_session(s.tenant) as db:
        assert (await db.execute(select(m.SecondFactor))).scalars().all() == []


async def test_email_enrolment_is_refused_when_the_policy_disallows_it(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(
        app_session, policy={"requirement": "admins", "methods": ["totp"], "grace_days": 7}
    )
    async with _http() as http:
        r = await http.post(_ENROLL, headers=s.token())
    assert r.status_code == 403, r.text
    assert outbox.sent == []


async def test_a_second_email_enrolment_is_a_conflict(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    async with _http() as http:
        r = await http.post(_ENROLL, headers=s.token())
    assert r.status_code == 409, r.text


async def test_the_enrol_token_can_enrol_email(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        r = await http.post(_ENROLL, headers=s.token("totp:enroll"))
        assert r.status_code == 200, r.text
        r = await http.post(
            _CONFIRM, json={"code": outbox.last_code()}, headers=s.token("totp:enroll")
        )
        assert r.status_code == 200, r.text


async def _hold_totp(app_session: AppSessionFactory, s: Seeded) -> None:
    async with app_session(s.tenant) as db:
        db.add(
            m.TotpCredential(
                tenant_id=s.tenant,
                member_id=s.member_id,
                secret_ref=f"totp:{s.member_id}",
                enrolled_at=dt.datetime.now(tz=dt.UTC),
                backup_codes=[],
            )
        )


async def test_the_enrol_token_cannot_add_email_once_the_member_has_a_method(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    """The enrol token proves the password only. It exists because the member
    had nothing to answer with; once they have, it must not add a factor."""
    s = await _seed(app_session)
    async with _http() as http:
        r = await http.post(_ENROLL, headers=s.token("totp:enroll"))
        assert r.status_code == 200, r.text
        code = outbox.last_code()
        await _hold_totp(app_session, s)
        refused_confirm = await http.post(
            _CONFIRM, json={"code": code}, headers=s.token("totp:enroll")
        )
        refused_enroll = await http.post(_ENROLL, headers=s.token("totp:enroll"))
        # A full session may still add one, with the same code.
        allowed = await http.post(_CONFIRM, json={"code": code}, headers=s.token())
    assert refused_confirm.status_code == 409, refused_confirm.text
    assert refused_enroll.status_code == 409, refused_enroll.text
    assert len(outbox.sent) == 1
    assert allowed.status_code == 200, allowed.text
    async with app_session(s.tenant) as db:
        assert len((await db.execute(select(m.SecondFactor))).scalars().all()) == 1


async def test_the_enrol_token_cannot_add_totp_once_the_member_has_a_method(
    app_session: AppSessionFactory,
) -> None:
    import pyotp

    s = await _seed(app_session, email_enrolled=True)
    secret = pyotp.random_base32()
    async with _http() as http:
        enroll = await http.post(_TOTP_ENROLL, headers=s.token("totp:enroll"))
        confirm = await http.post(
            _TOTP_CONFIRM,
            json={"secret": secret, "code": pyotp.TOTP(secret).now()},
            headers=s.token("totp:enroll"),
        )
    assert enroll.status_code == 409, enroll.text
    assert confirm.status_code == 409, confirm.text
    async with app_session(s.tenant) as db:
        assert (await db.execute(select(m.TotpCredential))).scalars().all() == []


# --- the login challenge -----------------------------------------------------


async def test_a_mailed_code_completes_the_challenge(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        r = await http.post(_SEND, headers=challenge)
        assert r.status_code == 200, r.text
        r = await http.post(_VERIFY, json={"code": outbox.last_code()}, headers=challenge)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["principal"]["scopes"] == []
        assert body["memberId"] == str(s.member_id)


async def test_a_wrong_code_is_refused_and_mints_nothing(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        await http.post(_SEND, headers=challenge)
        wrong = "000000" if outbox.last_code() != "000000" else "111111"
        r = await http.post(_VERIFY, json={"code": wrong}, headers=challenge)
    assert r.status_code == 401, r.text
    assert "token" not in r.json()


async def test_an_expired_code_is_refused(app_session: AppSessionFactory, outbox: Outbox) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        await http.post(_SEND, headers=challenge)
        await _age_challenges(
            app_session, s, expires_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1)
        )
        r = await http.post(_VERIFY, json={"code": outbox.last_code()}, headers=challenge)
    assert r.status_code == 401, r.text
    assert "token" not in r.json()


async def test_a_code_is_single_use(app_session: AppSessionFactory, outbox: Outbox) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        await http.post(_SEND, headers=challenge)
        code = outbox.last_code()
        first = await http.post(_VERIFY, json={"code": code}, headers=challenge)
        assert first.status_code == 200, first.text
        again = await http.post(_VERIFY, json={"code": code}, headers=challenge)
    assert again.status_code == 401, again.text
    assert "token" not in again.json()


async def test_the_right_code_is_refused_after_five_wrong_attempts(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        await http.post(_SEND, headers=challenge)
        code = outbox.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            r = await http.post(_VERIFY, json={"code": wrong}, headers=challenge)
            assert r.status_code == 401
        r = await http.post(_VERIFY, json={"code": code}, headers=challenge)
    assert r.status_code == 401, r.text
    assert "token" not in r.json()


async def test_the_right_code_still_works_on_the_fifth_attempt(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        await http.post(_SEND, headers=challenge)
        code = outbox.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(4):
            await http.post(_VERIFY, json={"code": wrong}, headers=challenge)
        r = await http.post(_VERIFY, json={"code": code}, headers=challenge)
    assert r.status_code == 200, r.text


async def test_a_new_code_needs_a_minute(app_session: AppSessionFactory, outbox: Outbox) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        assert (await http.post(_SEND, headers=challenge)).status_code == 200
        again = await http.post(_SEND, headers=challenge)
        assert again.status_code == 429, again.text
        assert len(outbox.sent) == 1
        await _age_challenges(
            app_session, s, created_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=61)
        )
        later = await http.post(_SEND, headers=challenge)
        assert later.status_code == 200, later.text
        # The new code replaces the old one: only the newest is accepted.
        old = outbox.sent[0]["body"]
        old_code = re.search(r"\b(\d{6})\b", old)
        assert old_code is not None
        if old_code.group(1) != outbox.last_code():
            r = await http.post(_VERIFY, json={"code": old_code.group(1)}, headers=challenge)
            assert r.status_code == 401


async def test_an_enrolment_code_is_useless_for_login(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        await http.post(_ENROLL, headers=s.token())
        enrol_code = outbox.last_code()
    async with app_session(s.tenant) as db:
        db.add(
            m.SecondFactor(
                tenant_id=s.tenant,
                member_id=s.member_id,
                method="email",
                data={"address": s.subject},
            )
        )
    async with _http() as http:
        r = await http.post(_VERIFY, json={"code": enrol_code}, headers=s.token("totp:challenge"))
    assert r.status_code == 401, r.text


async def test_email_cannot_answer_once_the_policy_removes_it(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    challenge = s.token("totp:challenge")
    async with _http() as http:
        await http.post(_SEND, headers=challenge)
        code = outbox.last_code()
        async with app_session(s.tenant) as db:
            org = await db.get(m.Organization, s.tenant)
            assert org is not None
            org.settings = {
                **org.settings,
                "two_factor": {"requirement": "optional", "methods": ["totp"], "grace_days": 7},
            }
        r = await http.post(_VERIFY, json={"code": code}, headers=challenge)
        assert r.status_code == 401, r.text
        r = await http.post(_SEND, headers=challenge)
        assert r.status_code == 403, r.text


async def test_a_failed_delivery_spends_the_code(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    outbox.fail = True
    async with _http() as http:
        r = await http.post(_SEND, headers=s.token("totp:challenge"))
        assert r.status_code == 502, r.text
        outbox.fail = False
        # Nothing was delivered, so the cooldown does not hold the retry back.
        r = await http.post(_SEND, headers=s.token("totp:challenge"))
        assert r.status_code == 200, r.text


async def test_backup_codes_work_for_a_member_whose_method_is_email(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        codes = await _enrol(http, s, outbox)
        r = await http.post(
            _TOTP_VERIFY, json={"code": codes[0]}, headers=s.token("totp:challenge")
        )
        assert r.status_code == 200, r.text
        again = await http.post(
            _TOTP_VERIFY, json={"code": codes[0]}, headers=s.token("totp:challenge")
        )
        assert again.status_code == 401


async def test_the_login_gate_challenges_an_email_member(
    app_session: AppSessionFactory,
) -> None:
    from oc8.auth.totp_gate import second_factor_gate

    s = await _seed(app_session, email_enrolled=True)
    async with app_session(s.tenant) as db:
        outcome = await second_factor_gate(db, member_id=s.member_id)
    assert outcome.requires_totp_code
    assert outcome.methods == ("email",)


async def test_totp_can_still_be_added_after_email(
    app_session: AppSessionFactory, outbox: Outbox
) -> None:
    import pyotp

    s = await _seed(app_session)
    async with _http() as http:
        await _enrol(http, s, outbox)
        secret = (await http.post("/api/v1/auth/totp/enroll", headers=s.token())).json()["secret"]
        r = await http.post(
            "/api/v1/auth/totp/confirm",
            json={"secret": secret, "code": pyotp.TOTP(secret).now()},
            headers=s.token(),
        )
        assert r.status_code == 200, r.text


# --- a changed sign-in address -------------------------------------------------


async def test_changing_the_sign_in_address_removes_the_email_method(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session, email_enrolled=True)
    admin = get_identity_provider().mint(tenant_id=s.tenant, subject="boss", role="org_admin")
    async with _http() as http:
        r = await http.put(
            f"/api/v1/members/{s.member_id}/subject",
            json={"subject": f"new-{s.subject}"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert r.status_code == 200, r.text
    async with app_session(s.tenant) as db:
        assert (await db.execute(select(m.SecondFactor))).scalars().all() == []


# --- review focus 4: where a pending token may go -------------------------------


def _request_for(path: str, token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


def _pending(scope: str) -> str:
    return get_identity_provider().mint(
        tenant_id=uuid.uuid4(), subject="p@example.com", role=MEMBER_ROLE, scopes=[scope]
    )


@pytest.mark.parametrize(
    ("scope", "path", "allowed"),
    [
        ("totp:enroll", _ENROLL, True),
        ("totp:enroll", _CONFIRM, True),
        ("totp:enroll", _SEND, False),
        ("totp:enroll", _VERIFY, False),
        ("totp:challenge", _SEND, True),
        ("totp:challenge", _VERIFY, True),
        ("totp:challenge", _TOTP_VERIFY, True),
        ("totp:challenge", _ENROLL, False),
        ("totp:challenge", _CONFIRM, False),
        ("totp:challenge", "/api/v1/settings/organization/two-factor", False),
        ("totp:enroll", "/api/v1/settings/organization/two-factor", False),
    ],
)
async def test_a_pending_token_reaches_only_its_own_routes(
    scope: str, path: str, allowed: bool
) -> None:
    request = _request_for(path, _pending(scope))
    if allowed:
        await deny_totp_pending_principals(request)
    else:
        with pytest.raises(HTTPException) as exc:
            await deny_totp_pending_principals(request)
        assert exc.value.status_code == 403
