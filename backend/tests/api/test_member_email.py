"""The members screens show the address a person signs in with, not their `sub`.

With an external OIDC provider the token's `sub` is often a uuid, and the
screen used to label that uuid "Sign-in email". `org_member.email` now carries
the token's `email` claim (display data only -- `subject` stays the identity),
and `GET /auth/config` names the identity provider's account page when the
deployment sets `OC8_ACCOUNT_MANAGEMENT_URL`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import jwt
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.auth.provider import _principal_from_claims
from oc8.config import Settings, get_settings
from oc8.main import create_app
from oc8.workspace.inviter import InviteResult, set_member_inviter
from oc8.workspace.members import looks_like_email
from tests.conftest import AppSessionFactory

OIDC_SUB = "5f0c2a8e-3b1d-4c7e-9a64-2d8f1e0b7c35"


def _token_with_claims(tenant: uuid.UUID, subject: str, **extra: Any) -> dict[str, str]:
    """A bearer token as an external OIDC provider would hand one over: the
    `sub` is opaque and the address travels in its own `email` claim."""
    settings = get_settings()
    now = dt.datetime.now(tz=dt.UTC)
    payload = {
        "sub": subject,
        "tenant_id": str(tenant),
        "role": "org_admin",
        "kind": "operator",
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
        **extra,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)
    return {"Authorization": f"Bearer {token}"}


def _admin(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="boss", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _member(app_session: AppSessionFactory, tenant: uuid.UUID, subject: str) -> m.OrgMember:
    async with app_session(tenant) as db:
        return (
            await db.execute(
                select(m.OrgMember).where(
                    m.OrgMember.subject == subject, m.OrgMember.deleted_at.is_(None)
                )
            )
        ).scalar_one()


# ----------------------------------------------------------------- the claim


def test_the_email_claim_is_read_from_the_token() -> None:
    tenant = uuid.uuid4()
    base = {"sub": OIDC_SUB, "tenant_id": str(tenant), "role": "org_admin"}

    assert _principal_from_claims({**base, "email": "ana@acme.test"}).email == "ana@acme.test"
    assert _principal_from_claims(base).email is None
    assert _principal_from_claims({**base, "email": "  "}).email is None
    assert _principal_from_claims({**base, "email": 42}).email is None
    # The identity is still the `sub`, whatever the address says.
    assert _principal_from_claims({**base, "email": "ana@acme.test"}).subject == OIDC_SUB


def test_only_an_address_shaped_subject_counts_as_an_email() -> None:
    assert looks_like_email("ana@acme.test")
    assert not looks_like_email("alice")
    assert not looks_like_email(OIDC_SUB)
    assert not looks_like_email("two@at@signs")


async def test_the_token_email_is_stored_and_follows_a_change(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with _http() as client:
        first = await client.get(
            "/api/v1/me", headers=_token_with_claims(tenant, OIDC_SUB, email="ana@acme.test")
        )
        assert first.status_code == 200, first.text
        assert first.json()["subject"] == OIDC_SUB
        assert first.json()["email"] == "ana@acme.test"
        assert (await _member(app_session, tenant, OIDC_SUB)).email == "ana@acme.test"

        # The person changed their address at the identity provider.
        changed = await client.get(
            "/api/v1/me", headers=_token_with_claims(tenant, OIDC_SUB, email="ana.b@acme.test")
        )
        assert changed.json()["email"] == "ana.b@acme.test"
        assert (await _member(app_session, tenant, OIDC_SUB)).email == "ana.b@acme.test"

        # A token without the claim does not wipe what is known.
        without = await client.get("/api/v1/me", headers=_token_with_claims(tenant, OIDC_SUB))
        assert without.json()["email"] == "ana.b@acme.test"

    member = await _member(app_session, tenant, OIDC_SUB)
    assert member.email == "ana.b@acme.test"
    # Still exactly one row: the email is never part of the lookup.
    async with app_session(tenant) as db:
        rows = (await db.execute(select(m.OrgMember.id))).scalars().all()
    assert len(rows) == 1


# ------------------------------------------------------------------- the DTOs


async def test_the_member_list_and_detail_carry_the_email(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with _http() as client:
        await client.get(
            "/api/v1/me", headers=_token_with_claims(tenant, OIDC_SUB, email="ana@acme.test")
        )
        await client.get("/api/v1/me", headers=_admin(tenant))

        listed = await client.get("/api/v1/members", headers=_admin(tenant))
        assert listed.status_code == 200, listed.text
        by_subject = {row["subject"]: row for row in listed.json()["items"]}
        assert by_subject[OIDC_SUB]["email"] == "ana@acme.test"
        # A dev-login subject never had an address: `null`, not the subject.
        assert by_subject["boss"]["email"] is None

        searched = await client.get(
            "/api/v1/members", params={"search": "ana@acme"}, headers=_admin(tenant)
        )
        assert [row["subject"] for row in searched.json()["items"]] == [OIDC_SUB]

        # A write that answers with one member answers with its email too.
        member_id = by_subject[OIDC_SUB]["id"]
        renamed = await client.post(
            "/api/v1/members",
            json={"subject": OIDC_SUB, "displayName": "Ana"},
            headers=_admin(tenant),
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["id"] == member_id
        assert renamed.json()["email"] == "ana@acme.test"


async def test_an_external_invitation_stores_the_invited_address(
    app_session: AppSessionFactory,
) -> None:
    class _Inviter:
        async def invite(self, **kwargs: Any) -> InviteResult:
            return InviteResult(subject=str(uuid.uuid4()))

        async def remove(self, **kwargs: Any) -> None:
            return None

    tenant = uuid.uuid4()
    set_member_inviter(_Inviter())
    try:
        async with _http() as client:
            created = await client.post(
                "/api/v1/members",
                json={"subject": "kim@acme.test", "displayName": "Kim", "role": "operator"},
                headers=_admin(tenant),
            )
    finally:
        set_member_inviter(None)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email"] == "kim@acme.test"
    assert body["subject"] != "kim@acme.test"


# ------------------------------------------------------------ local sign-in


async def test_local_sign_in_keeps_the_subject_as_the_email(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with _http() as client:
        created = await client.post(
            "/api/v1/members",
            json={"subject": "lea@acme.test", "displayName": "Lea", "password": "a long password"},
            headers=_admin(tenant),
        )
        assert created.status_code == 201, created.text
        assert created.json()["subject"] == "lea@acme.test"
        assert created.json()["email"] == "lea@acme.test"

        renamed = await client.put(
            f"/api/v1/members/{created.json()['id']}/subject",
            json={"subject": "lea.neu@acme.test"},
            headers=_admin(tenant),
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["subject"] == "lea.neu@acme.test"
        assert renamed.json()["email"] == "lea.neu@acme.test"

        # A dev-login style subject is not an address and is not shown as one.
        plain = await client.post(
            "/api/v1/members",
            json={"subject": "lea-dev", "displayName": "Lea"},
            headers=_admin(tenant),
        )
        assert plain.json()["email"] is None

    member = await _member(app_session, tenant, "lea.neu@acme.test")
    assert member.email == "lea.neu@acme.test"


# ---------------------------------------------------------- the auth config


async def _auth_config() -> dict[str, Any]:
    async with _http() as client:
        r = await client.get("/api/v1/auth/config")
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body


async def test_the_account_page_is_not_offered_unless_configured() -> None:
    body = await _auth_config()
    assert body["accountManagementUrl"] is None


@pytest.mark.parametrize("env", ["dev", "production"])
async def test_the_account_page_reaches_the_browser_when_configured(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://id.example.test/realms/acme/account"
    settings = Settings(env=env, account_management_url=url)
    monkeypatch.setattr("oc8.api.v1.auth.get_settings", lambda: settings)

    body = await _auth_config()

    assert body["accountManagementUrl"] == url


def test_the_setting_reads_its_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_ACCOUNT_MANAGEMENT_URL", "https://id.example.test/account")
    assert Settings().account_management_url == "https://id.example.test/account"
