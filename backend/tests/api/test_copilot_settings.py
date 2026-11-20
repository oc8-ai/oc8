from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def test_copilot_settings_read_write_and_validate(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        s.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:6]}",
                name="T",
                tier="standard",
                region="eu",
                settings={},
            )
        )
        await s.flush()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            got = await c.get("/api/v1/settings/copilot", headers=_headers(tenant))
            assert got.status_code == 200 and got.json() == {"maxActiveFollowups": 20}

            forbidden = await c.put(
                "/api/v1/settings/copilot",
                json={"maxActiveFollowups": 5},
                headers=_headers(tenant, "operator"),
            )
            assert forbidden.status_code == 403

            for bad in (0, 101):
                r = await c.put(
                    "/api/v1/settings/copilot",
                    json={"maxActiveFollowups": bad},
                    headers=_headers(tenant),
                )
                assert r.status_code == 422

            put = await c.put(
                "/api/v1/settings/copilot",
                json={"maxActiveFollowups": 5},
                headers=_headers(tenant),
            )
            assert put.status_code == 200 and put.json() == {"maxActiveFollowups": 5}
            got = await c.get("/api/v1/settings/copilot", headers=_headers(tenant))
            assert got.json() == {"maxActiveFollowups": 5}
