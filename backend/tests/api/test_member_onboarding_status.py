"""A member's own welcome: `GET /me` reports it and two self-service routes end
it, for the caller's own row only and without any extra permission."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, role: str, subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _seed_org(app_session: AppSessionFactory) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={}))
        await db.flush()
    return tenant


async def test_a_new_member_is_pending_on_me(app_session: AppSessionFactory) -> None:
    tenant = await _seed_org(app_session)
    async with _http() as http:
        resp = await http.get("/api/v1/me", headers=_headers(tenant, "member", "ana@acme.test"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["memberOnboardingStatus"] == "pending"


async def test_complete_and_skip_set_the_callers_own_status(app_session: AppSessionFactory) -> None:
    tenant = await _seed_org(app_session)
    ana = _headers(tenant, "member", "ana@acme.test")
    ben = _headers(tenant, "member", "ben@acme.test")
    async with _http() as http:
        done = await http.post("/api/v1/onboarding/member/complete", headers=ana)
        skipped = await http.post("/api/v1/onboarding/member/skip", headers=ben)
        assert done.status_code == 200, done.text
        assert done.json() == {"memberOnboardingStatus": "completed"}
        assert skipped.json() == {"memberOnboardingStatus": "skipped"}
        assert (await http.get("/api/v1/me", headers=ana)).json()[
            "memberOnboardingStatus"
        ] == "completed"
        assert (await http.get("/api/v1/me", headers=ben)).json()[
            "memberOnboardingStatus"
        ] == "skipped"


async def test_completing_does_not_touch_anybody_else(app_session: AppSessionFactory) -> None:
    tenant = await _seed_org(app_session)
    ana = _headers(tenant, "org_admin", "ana@acme.test")
    ben = _headers(tenant, "member", "ben@acme.test")
    async with _http() as http:
        await http.get("/api/v1/me", headers=ben)  # mints ben's row
        await http.post("/api/v1/onboarding/member/complete", headers=ana)
        assert (await http.get("/api/v1/me", headers=ben)).json()[
            "memberOnboardingStatus"
        ] == "pending"


async def test_the_organisation_status_is_unchanged(app_session: AppSessionFactory) -> None:
    tenant = await _seed_org(app_session)
    async with _http() as http:
        headers = _headers(tenant, "org_admin", "ana@acme.test")
        await http.post("/api/v1/onboarding/member/complete", headers=headers)
        assert (await http.get("/api/v1/me", headers=headers)).json()[
            "onboardingStatus"
        ] == "pending"
