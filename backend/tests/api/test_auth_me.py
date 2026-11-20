from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(role: str) -> str:
    return get_identity_provider().mint(
        tenant_id=uuid.UUID(str(ACME_TENANT_ID)), subject="u", role=role
    )


async def _me(token: str | None) -> tuple[int, dict[str, object]]:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            r = await client.get("/api/v1/me", headers=headers)
            return r.status_code, (r.json() if r.status_code == 200 else {})


async def test_me_returns_org_admin_role() -> None:
    status, body = await _me(_token("org_admin"))
    assert status == 200, body
    assert body["role"] == "org_admin"


async def test_me_returns_member_role() -> None:
    status, body = await _me(_token("member"))
    assert status == 200
    assert body["role"] == "member"


async def test_me_requires_auth() -> None:
    status, _ = await _me(None)
    assert status == 401


async def test_me_returns_onboarding_status_pending_for_fresh_acme(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        if await db.get(m.Organization, tenant) is None:
            db.add(m.Organization(id=tenant, slug=f"test-{uuid.uuid4().hex}", name="Test"))
            await db.flush()
    status, body = await _me(_token("org_admin"))
    assert status == 200, body
    assert body["onboardingStatus"] in ("pending", "completed", "skipped")


async def test_me_names_the_organisations_timezone(app_session: AppSessionFactory) -> None:
    """The schedule editor defaults a new trigger to it."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        if org is None:
            org = m.Organization(id=tenant, slug=f"test-{uuid.uuid4().hex}", name="Test")
            db.add(org)
            await db.flush()
        expected = org.timezone
    status, body = await _me(_token("org_admin"))
    assert status == 200, body
    assert body["organisationTimezone"] == expected
