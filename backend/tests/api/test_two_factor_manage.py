"""Listing and removing one's own second factors, an administrator's reset,
and the audit events every two-factor action leaves."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pyotp
import pytest
from asgi_lifespan import LifespanManager
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

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

PASSWORD = "Whatever123!"
_METHODS = "/api/v1/auth/two-factor/methods"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@pytest.fixture
def mailed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    bodies: list[str] = []

    async def _resolve(db: Any, *, tenant_id: uuid.UUID) -> SmtpConfig:
        return SmtpConfig(
            tenant_id=tenant_id,
            credential_id=uuid.uuid4(),
            host="h",
            port=1,
            username="",
            password="",
            use_tls=False,
            from_address="a@b.c",
        )

    async def _deliver(config: SmtpConfig, *, to: str, subject: str, body: str) -> bool:
        bodies.append(body)
        return True

    monkeypatch.setattr(two_factor, "resolve_smtp_config", _resolve)
    monkeypatch.setattr(two_factor, "deliver", _deliver)
    return bodies


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

    def headers(self, *scopes: str, role: str = MEMBER_ROLE) -> dict[str, str]:
        token = get_identity_provider().mint(
            tenant_id=self.tenant, subject=self.subject, role=role, scopes=list(scopes)
        )
        return {"Authorization": f"Bearer {token}"}


async def _seed(
    app_session: AppSessionFactory,
    *,
    policy: dict[str, Any] | None = None,
    admin: bool = False,
    tenant: uuid.UUID | None = None,
) -> Seeded:
    tenant = tenant or uuid.uuid4()
    subject = f"m-{uuid.uuid4().hex[:8]}@example.com"
    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        if org is None:
            settings: dict[str, Any] = {"active_smtp_credential_id": str(uuid.uuid4())}
            if policy is not None:
                settings["two_factor"] = policy
            db.add(
                m.Organization(id=tenant, slug=f"o-{tenant.hex[:8]}", name="O", settings=settings)
            )
        role_id = None
        if admin:
            role = (
                await db.execute(select(m.Role).where(m.Role.name == "org_admin"))
            ).scalar_one_or_none()
            if role is None:
                role = m.Role(tenant_id=tenant, name="org_admin", builtin=True)
                db.add(role)
                await db.flush()
            role_id = role.id
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid4(),
            email=subject,
            password_hash=hash_password(PASSWORD),
            role_id=role_id,
        )
        db.add(member)
        await db.flush()
        return Seeded(tenant=tenant, member_id=member.id, subject=subject)


async def _enrol_totp(http: AsyncClient, s: Seeded) -> str:
    secret: str = (await http.post("/api/v1/auth/totp/enroll", headers=s.headers())).json()[
        "secret"
    ]
    r = await http.post(
        "/api/v1/auth/totp/confirm",
        json={"secret": secret, "code": pyotp.TOTP(secret).now()},
        headers=s.headers(),
    )
    assert r.status_code == 200, r.text
    return secret


async def _enrol_email(http: AsyncClient, s: Seeded, mailed: list[str]) -> None:
    import re

    r = await http.post("/api/v1/auth/two-factor/email/enroll", headers=s.headers())
    assert r.status_code == 200, r.text
    match = re.search(r"\b(\d{6})\b", mailed[-1])
    assert match is not None
    r = await http.post(
        "/api/v1/auth/two-factor/email/confirm",
        json={"code": match.group(1)},
        headers=s.headers(),
    )
    assert r.status_code == 200, r.text


async def _remove(http: AsyncClient, s: Seeded, method: str, password: str = PASSWORD) -> Any:
    return await http.request(
        "DELETE",
        f"{_METHODS}/{method}",
        json={"currentPassword": password},
        headers=s.headers(),
    )


async def _events(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.AuditEvent]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action.like("two_factor.%"),
                    )
                )
            ).scalars()
        )


# --- listing -------------------------------------------------------------------


async def test_methods_lists_what_the_caller_holds(
    app_session: AppSessionFactory, mailed: list[str]
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        empty = await http.get(_METHODS, headers=s.headers())
        assert empty.status_code == 200, empty.text
        assert empty.json()["methods"] == []
        await _enrol_totp(http, s)
        await _enrol_email(http, s, mailed)
        r = await http.get(_METHODS, headers=s.headers())
    body = r.json()
    assert [x["method"] for x in body["methods"]] == ["totp", "email"]
    assert all(x["allowed"] and x["removable"] for x in body["methods"])
    assert body["required"] is False
    assert body["allowedMethods"] == ["totp", "email", "passkey"]
    assert body["enrollableMethods"] == ["totp", "email", "passkey"]
    assert body["passkeys"] == []
    assert body["backupCodesLeft"] == 10


# --- self-service removal ------------------------------------------------------


async def test_removal_needs_the_current_password(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        await _enrol_totp(http, s)
        r = await _remove(http, s, "totp", password="wrong-password")
        assert r.status_code == 401, r.text
        assert [
            x["method"] for x in (await http.get(_METHODS, headers=s.headers())).json()["methods"]
        ] == ["totp"]


async def test_removing_the_last_method_is_refused_while_the_policy_requires_it(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(
        app_session,
        policy={"requirement": "everyone", "methods": ["totp", "email"], "grace_days": 7},
    )
    async with _http() as http:
        await _enrol_totp(http, s)
        listed = (await http.get(_METHODS, headers=s.headers())).json()
        assert listed["required"] is True
        assert listed["methods"][0]["removable"] is False
        r = await _remove(http, s, "totp")
    assert r.status_code == 409, r.text


async def test_removing_one_of_two_methods_is_allowed_while_required(
    app_session: AppSessionFactory, mailed: list[str]
) -> None:
    s = await _seed(
        app_session,
        policy={"requirement": "everyone", "methods": ["totp", "email"], "grace_days": 7},
    )
    async with _http() as http:
        await _enrol_totp(http, s)
        await _enrol_email(http, s, mailed)
        r = await _remove(http, s, "totp")
        assert r.status_code == 204, r.text
        left = (await http.get(_METHODS, headers=s.headers())).json()
    assert [x["method"] for x in left["methods"]] == ["email"]
    async with app_session(s.tenant) as db:
        cred = (await db.execute(select(m.TotpCredential))).scalar_one()
        assert cred.enrolled_at is None
        assert cred.backup_codes  # still there for the email method


async def test_removing_the_last_method_when_optional_removes_the_backup_codes(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(
        app_session, policy={"requirement": "optional", "methods": ["totp"], "grace_days": 7}
    )
    async with _http() as http:
        await _enrol_totp(http, s)
        r = await _remove(http, s, "totp")
        assert r.status_code == 204, r.text
    async with app_session(s.tenant) as db:
        assert (await db.execute(select(m.TotpCredential))).scalars().all() == []
        assert (await db.execute(select(m.Secret))).scalars().all() == []
    events = await _events(app_session, s.tenant)
    removed = [e for e in events if e.action == "two_factor.removed"]
    assert len(removed) == 1
    assert removed[0].category == "member"
    assert removed[0].resource == {"member_id": str(s.member_id), "method": "totp"}


async def test_removing_a_method_not_held_is_404(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        r = await _remove(http, s, "email")
    assert r.status_code == 404, r.text


async def test_an_admin_left_without_a_method_has_their_grace_clock_started(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(
        app_session,
        admin=True,
        policy={"requirement": "optional", "methods": ["totp"], "grace_days": 7},
    )
    async with _http() as http:
        await _enrol_totp(http, s)
        assert (await _remove(http, s, "totp")).status_code == 204
    async with app_session(s.tenant) as db:
        member = await db.get(m.OrgMember, s.member_id)
        assert member is not None and member.totp_grace_started_at is not None


# --- admin reset ---------------------------------------------------------------


async def test_an_admin_resets_a_members_second_factors(
    app_session: AppSessionFactory, mailed: list[str]
) -> None:
    s = await _seed(app_session)
    admin = await _seed(app_session, admin=True, tenant=s.tenant)
    async with _http() as http:
        await _enrol_totp(http, s)
        await _enrol_email(http, s, mailed)
        r = await http.post(
            f"/api/v1/members/{s.member_id}/two-factor/reset",
            headers=admin.headers(role="org_admin"),
        )
        assert r.status_code == 204, r.text
        left = (await http.get(_METHODS, headers=s.headers())).json()
    assert left["methods"] == []
    assert left["backupCodesLeft"] == 0
    async with app_session(s.tenant) as db:
        member = await db.get(m.OrgMember, s.member_id)
        assert member is not None and member.totp_grace_started_at is not None
        assert (await db.execute(select(m.SecondFactor))).scalars().all() == []
        assert (await db.execute(select(m.TotpCredential))).scalars().all() == []
    reset = [e for e in await _events(app_session, s.tenant) if e.action == "two_factor.reset"]
    assert len(reset) == 1
    assert reset[0].actor_id == admin.member_id
    assert reset[0].resource == {"member_id": str(s.member_id)}


async def test_an_admin_cannot_reset_themselves(app_session: AppSessionFactory) -> None:
    admin = await _seed(app_session, admin=True)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/members/{admin.member_id}/two-factor/reset",
            headers=admin.headers(role="org_admin"),
        )
    assert r.status_code == 409, r.text


async def test_reset_needs_member_manage(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    other = await _seed(app_session, tenant=s.tenant)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/members/{s.member_id}/two-factor/reset", headers=other.headers()
        )
    assert r.status_code == 403, r.text


# --- audit ---------------------------------------------------------------------


async def test_enrolment_and_backup_code_regeneration_are_audited(
    app_session: AppSessionFactory, mailed: list[str]
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        secret = await _enrol_totp(http, s)
        await _enrol_email(http, s, mailed)
        r = await http.post("/api/v1/auth/totp/regenerate-backup-codes", headers=s.headers())
        assert r.status_code == 200, r.text
    events = await _events(app_session, s.tenant)
    assert sorted((e.action, (e.resource or {}).get("method")) for e in events) == [
        ("two_factor.backup_codes_regenerated", None),
        ("two_factor.enrolled", "email"),
        ("two_factor.enrolled", "totp"),
    ]
    for event in events:
        assert event.category == "member"
        assert secret not in str(event.resource)


async def test_the_fifth_wrong_email_code_is_audited_as_locked(
    app_session: AppSessionFactory, mailed: list[str]
) -> None:
    import re

    s = await _seed(app_session)
    async with _http() as http:
        await _enrol_email(http, s, mailed)
        challenge = s.headers("totp:challenge")
        await http.post("/api/v1/auth/two-factor/email/send", headers=challenge)
        match = re.search(r"\b(\d{6})\b", mailed[-1])
        assert match is not None
        wrong = "000000" if match.group(1) != "000000" else "111111"
        for _ in range(5):
            await http.post(
                "/api/v1/auth/two-factor/email/verify", json={"code": wrong}, headers=challenge
            )
    locked = [e for e in await _events(app_session, s.tenant) if e.action == "two_factor.locked"]
    assert len(locked) == 1
    assert locked[0].resource == {"member_id": str(s.member_id), "method": "email"}
    assert wrong not in str(locked[0].resource) and match.group(1) not in str(locked[0].resource)


async def test_regenerating_needs_a_method(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        r = await http.post("/api/v1/auth/totp/regenerate-backup-codes", headers=s.headers())
    assert r.status_code == 404, r.text


async def test_an_email_only_member_can_regenerate_backup_codes(
    app_session: AppSessionFactory, mailed: list[str]
) -> None:
    s = await _seed(app_session)
    async with _http() as http:
        await _enrol_email(http, s, mailed)
        r = await http.post("/api/v1/auth/totp/regenerate-backup-codes", headers=s.headers())
    assert r.status_code == 200, r.text
    assert len(r.json()["backupCodes"]) == 10


# --- review focus 4: the new routes are full-session only -----------------------


def _request_for(method: str, path: str, token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


@pytest.mark.parametrize("scope", ["totp:enroll", "totp:challenge"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", _METHODS),
        ("DELETE", f"{_METHODS}/totp"),
        ("DELETE", f"{_METHODS}/email"),
        ("POST", f"/api/v1/members/{uuid.uuid4()}/two-factor/reset"),
        ("POST", "/api/v1/auth/totp/regenerate-backup-codes"),
    ],
)
async def test_a_pending_token_cannot_manage_second_factors(
    scope: str, method: str, path: str
) -> None:
    token = get_identity_provider().mint(
        tenant_id=uuid.uuid4(), subject="p@example.com", role="org_admin", scopes=[scope]
    )
    with pytest.raises(HTTPException) as exc:
        await deny_totp_pending_principals(_request_for(method, path, token))
    assert exc.value.status_code == 403
