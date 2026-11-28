"""`POST /auth/session/renew`: a password session that is in use stays signed in.

A Community token lived exactly `jwt_ttl_seconds` (1 hour) with no way to
extend it, so whoever was still at work an hour after signing in -- halfway
through configuring an agent, say -- was thrown out on the next request and
lost everything unsaved. While a full session is still valid it can now be
swapped for a fresh one, up to `session_max_seconds` after the sign-in, for
a member that still exists."""

from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _member(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, deleted: bool = False
) -> str:
    subject = f"anna-{uuid.uuid4()}@example.com"
    async with app_session(tenant) as db:
        db.add(
            m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=uuid.uuid4(),
                display_name="Anna",
                all_departments=True,
                deleted_at=dt.datetime.now(tz=dt.UTC) if deleted else None,
            )
        )
        await db.commit()
    return subject


def _claims(token: str) -> dict[str, object]:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_alg])


def _token(tenant: uuid.UUID, subject: str, **claims: object) -> str:
    s = get_settings()
    now = int(dt.datetime.now(tz=dt.UTC).timestamp())
    payload = {
        "sub": subject,
        "tenant_id": str(tenant),
        "role": "member",
        "kind": "operator",
        "scopes": [],
        "iat": now,
        "exp": now + 600,
        **claims,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_alg)


async def _renew(token: str) -> tuple[int, dict[str, object]]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/auth/session/renew", headers={"Authorization": f"Bearer {token}"}
            )
    return r.status_code, (r.json() if r.content else {})


async def test_a_valid_session_gets_a_fresh_token_that_keeps_the_sign_in_time(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    subject = await _member(app_session, tenant)
    signed_in = int(dt.datetime.now(tz=dt.UTC).timestamp()) - 3000
    old = _token(tenant, subject, auth_time=signed_in, iat=signed_in)

    code, body = await _renew(old)

    assert code == 200, body
    new = _claims(str(body["token"]))
    assert new["sub"] == subject and new["tenant_id"] == str(tenant)
    assert new["role"] == "member" and new["scopes"] == []
    assert new["auth_time"] == signed_in
    assert int(new["exp"]) > int(_claims(old)["exp"])  # type: ignore[call-overload]


async def test_a_token_minted_at_sign_in_carries_its_sign_in_time() -> None:
    token = get_identity_provider().mint(tenant_id=uuid.uuid4(), subject="a", role="member")
    claims = _claims(token)
    assert claims["auth_time"] == claims["iat"]


async def test_renewal_stops_once_the_sign_in_is_older_than_the_maximum(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    subject = await _member(app_session, tenant)
    long_ago = int(dt.datetime.now(tz=dt.UTC).timestamp()) - get_settings().session_max_seconds - 1
    code, _ = await _renew(_token(tenant, subject, auth_time=long_ago))
    assert code == 401


async def test_a_challenge_token_cannot_be_renewed_into_a_session(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    subject = await _member(app_session, tenant)
    code, _ = await _renew(_token(tenant, subject, scopes=["totp:challenge"]))
    assert code == 403


async def test_a_removed_member_is_not_renewed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    subject = await _member(app_session, tenant, deleted=True)
    code, _ = await _renew(_token(tenant, subject))
    assert code == 401


async def test_an_agent_token_is_not_renewed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    subject = await _member(app_session, tenant)
    code, _ = await _renew(_token(tenant, subject, kind="agent"))
    assert code in (401, 403)
