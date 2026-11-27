from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.agents.hire import create_hire_request
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _org(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    async with app_session(tenant) as s:
        s.add(m.Organization(id=tenant, slug=f"t{tenant.hex[:6]}", name="T",
                             tier="standard", region="eu", settings={}))
        await s.flush()


async def test_toggle_admin_only_and_reflects(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _org(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            # non-admin cannot flip it
            forbidden = await client.put(
                "/api/v1/settings/hire-approval",
                json={"enabled": True},
                headers=_headers(tenant, "operator"),
            )
            assert forbidden.status_code == 403
            # admin can
            put = await client.put("/api/v1/settings/hire-approval",
                                   json={"enabled": True}, headers=_headers(tenant))
            assert put.status_code == 200 and put.json()["enabled"] is True
            got = await client.get("/api/v1/settings/hire-approval", headers=_headers(tenant))
            assert got.json()["enabled"] is True


async def test_approve_activates_reject_soft_deletes(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        a1 = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A1",
                     status="pending_approval")
        a2 = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A2",
                     status="pending_approval")
        s.add_all([a1, a2])
        await s.flush()
        r1 = await create_hire_request(s, agent=a1)
        r2 = await create_hire_request(s, agent=a2)
        r1_id, r2_id = r1.id, r2.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            ok = await client.post(f"/api/v1/approvals/{r1_id}/decision",
                                   json={"decision": "approve"}, headers=_headers(tenant))
            assert ok.status_code == 200
            no = await client.post(f"/api/v1/approvals/{r2_id}/decision",
                                   json={"decision": "reject"}, headers=_headers(tenant))
            assert no.status_code == 200

    async with app_session(tenant) as s:
        agent1 = await s.get(m.Agent, a1.id)
        agent2 = await s.get(m.Agent, a2.id)
        assert agent1 is not None and agent1.status == "idle" and agent1.deleted_at is None
        assert agent2 is not None and agent2.deleted_at is not None
