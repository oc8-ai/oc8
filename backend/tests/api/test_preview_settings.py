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


async def test_preview_scripts_default_on_readable_by_members_set_by_admins(
    app_session: AppSessionFactory,
) -> None:
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
            # The chat reads it for every member, so a member may read it.
            got = await c.get("/api/v1/settings/previews", headers=_headers(tenant, "member"))
            assert got.status_code == 200 and got.json() == {"runScripts": True}

            forbidden = await c.put(
                "/api/v1/settings/previews",
                json={"runScripts": False},
                headers=_headers(tenant, "member"),
            )
            assert forbidden.status_code == 403

            put = await c.put(
                "/api/v1/settings/previews", json={"runScripts": False}, headers=_headers(tenant)
            )
            assert put.status_code == 200 and put.json() == {"runScripts": False}
            got = await c.get("/api/v1/settings/previews", headers=_headers(tenant, "member"))
            assert got.json() == {"runScripts": False}

    async with app_session(tenant) as s:
        org = await s.get(m.Organization, tenant)
        assert org is not None and org.settings["preview_scripts"] is False
        events = (
            await s.execute(
                m.AuditEvent.__table__.select().where(
                    m.AuditEvent.action == "organization.settings.updated"
                )
            )
        ).all()
        assert len(events) == 1


async def test_preview_scripts_setting_is_per_tenant(app_session: AppSessionFactory) -> None:
    off, other = uuid.uuid4(), uuid.uuid4()
    for tenant, settings in ((off, {"preview_scripts": False}), (other, {})):
        async with app_session(tenant) as s:
            s.add(
                m.Organization(
                    id=tenant,
                    slug=f"t{tenant.hex[:6]}",
                    name="T",
                    tier="standard",
                    region="eu",
                    settings=settings,
                )
            )
            await s.flush()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            mine = await c.get("/api/v1/settings/previews", headers=_headers(off, "member"))
            theirs = await c.get("/api/v1/settings/previews", headers=_headers(other, "member"))
    # One tenant turning scripts off says nothing to, and changes nothing for, another.
    assert mine.json() == {"runScripts": False}
    assert theirs.json() == {"runScripts": True}
