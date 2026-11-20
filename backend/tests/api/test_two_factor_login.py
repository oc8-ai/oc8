"""The login side of the two-factor policy: the methods named in the login
response, and a method removed from the policy that can no longer be used
(review focus 2) -- not to answer a challenge, not to enrol."""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import MEMBER_ROLE
from oc8.constants import ACME_TENANT_ID
from tests.auth import test_password_auth as _password_auth

pytestmark = pytest.mark.asyncio

PASSWORD = "SecurePassword123!"

# The singleton-organisation fixtures `/auth/setup` and `/auth/login` need.
app_with_db = _password_auth.app_with_db
client = _password_auth.client
db_session = _password_auth.db_session
org_in_db = _password_auth.org_in_db


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_with_totp(client: AsyncClient, email: str) -> str:
    """Set up the instance's admin and enrol TOTP; returns the secret."""
    setup = await client.post(
        "/api/v1/auth/setup", json={"email": email, "password": PASSWORD, "displayName": "A"}
    )
    assert setup.status_code == 201, setup.text
    token = setup.json()["token"]
    secret: str = (await client.post("/api/v1/auth/totp/enroll", headers=_auth(token))).json()[
        "secret"
    ]
    confirm = await client.post(
        "/api/v1/auth/totp/confirm",
        json={"secret": secret, "code": pyotp.TOTP(secret).now()},
        headers=_auth(token),
    )
    assert confirm.status_code == 200, confirm.text
    return secret


async def _set_policy(db: AsyncSession, policy: dict[str, Any]) -> None:
    org = await db.get(m.Organization, ACME_TENANT_ID)
    assert org is not None
    org.settings = {**org.settings, "two_factor": policy}
    await db.commit()


async def test_the_challenge_response_names_the_members_methods(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    await _admin_with_totp(client, "methods@example.com")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "methods@example.com", "password": PASSWORD}
    )
    body = login.json()
    assert body["requiresTotpCode"] is True
    assert body["twoFactorMethods"] == ["totp"]


async def test_the_enrolment_response_names_what_can_be_enrolled(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "enrol-methods@example.com", "password": PASSWORD, "displayName": "A"},
    )
    assert setup.status_code == 201
    await db_session.execute(
        update(m.OrgMember)
        .where(m.OrgMember.subject == "enrol-methods@example.com")
        .values(totp_grace_started_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=30))
    )
    await db_session.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"email": "enrol-methods@example.com", "password": PASSWORD}
    )
    body = login.json()
    assert body["requiresTotpEnrollment"] is True
    # No mail server: email cannot be enrolled; passkey is never offered.
    assert body["twoFactorMethods"] == ["totp"]


async def test_a_full_session_names_no_methods(
    client: AsyncClient, org_in_db: m.Organization
) -> None:
    await client.post(
        "/api/v1/auth/setup",
        json={"email": "full@example.com", "password": PASSWORD, "displayName": "A"},
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": "full@example.com", "password": PASSWORD}
    )
    assert login.json()["twoFactorMethods"] == []


async def test_off_gives_an_enrolled_member_a_full_session(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    await _admin_with_totp(client, "off@example.com")
    await _set_policy(db_session, {"requirement": "off", "methods": ["totp"], "grace_days": 7})
    login = await client.post(
        "/api/v1/auth/login", json={"email": "off@example.com", "password": PASSWORD}
    )
    body = login.json()
    assert body["requiresTotpCode"] is False
    assert body["requiresTotpEnrollment"] is False
    assert body["principal"]["scopes"] == []


async def test_a_removed_method_cannot_answer_a_challenge(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    """Review focus 2: a challenge token minted before TOTP was removed from
    the policy, plus a correct TOTP code, still mints nothing."""
    secret = await _admin_with_totp(client, "removed@example.com")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "removed@example.com", "password": PASSWORD}
    )
    challenge = login.json()["token"]
    await _set_policy(
        db_session, {"requirement": "optional", "methods": ["passkey"], "grace_days": 7}
    )
    r = await client.post(
        "/api/v1/auth/totp/verify",
        json={"code": pyotp.TOTP(secret).now()},
        headers=_auth(challenge),
    )
    assert r.status_code == 401, r.text
    assert "token" not in r.json()


async def test_a_removed_method_cannot_be_enrolled(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    setup = await client.post(
        "/api/v1/auth/setup",
        json={"email": "no-totp@example.com", "password": PASSWORD, "displayName": "A"},
    )
    token = setup.json()["token"]
    await _set_policy(db_session, {"requirement": "off", "methods": ["passkey"], "grace_days": 7})
    r = await client.post("/api/v1/auth/totp/enroll", headers=_auth(token))
    assert r.status_code == 403, r.text
    secret = pyotp.random_base32()
    r = await client.post(
        "/api/v1/auth/totp/confirm",
        json={"secret": secret, "code": pyotp.TOTP(secret).now()},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text


async def test_a_challenge_token_for_a_member_without_methods_mints_nothing(
    client: AsyncClient, db_session: AsyncSession, org_in_db: m.Organization
) -> None:
    await client.post(
        "/api/v1/auth/setup",
        json={"email": "nothing@example.com", "password": PASSWORD, "displayName": "A"},
    )
    token = get_identity_provider().mint(
        tenant_id=ACME_TENANT_ID,
        subject="nothing@example.com",
        role=MEMBER_ROLE,
        scopes=["totp:challenge"],
    )
    r = await client.post(
        "/api/v1/auth/totp/verify", json={"code": "ABCDE12345"}, headers=_auth(token)
    )
    assert r.status_code == 401
