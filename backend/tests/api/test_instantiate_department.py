from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.capas.service import install_plugin
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


def _manifest(ptype: str = "department_template") -> dict[str, Any]:
    return {
        "name": "sales",
        "version": "1.0.0",
        "type": ptype,
        "department_template": {
            "frame": {"tools": {}, "kbs": [], "memory": {}},
            "agents": [
                {"name": "Head of Sales", "is_team_lead": True, "persona": "# Lead"},
                {"name": "Rep A", "reports_to": "Head of Sales"},
            ],
        },
    }


async def _install(app_session: AppSessionFactory, tenant: uuid.UUID, ptype: str) -> uuid.UUID:
    async with app_session(tenant) as s:
        version = await install_plugin(s, tenant_id=tenant, manifest_data=_manifest(ptype))
        return version.capa_id


async def test_instantiate_department_endpoint(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    plugin_id = await _install(app_session, tenant, "department_template")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/capas/{plugin_id}/instantiate-department",
                json={"name": "Sales EU"},
                headers=_headers(tenant),
            )
            assert r.status_code == 201
            assert r.json()["name"] == "Sales EU"
            dept_id = uuid.UUID(r.json()["id"])

    async with app_session(tenant) as s:
        agents = (
            (await s.execute(select(m.Agent).where(m.Agent.department_id == dept_id)))
            .scalars()
            .all()
        )
        assert len(agents) == 2 and all(a.status == "idle" for a in agents)


async def test_instantiate_department_wrong_type_400(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    plugin_id = await _install(app_session, tenant, "agent_template")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/capas/{plugin_id}/instantiate-department",
                json={"name": "X"},
                headers=_headers(tenant),
            )
            assert r.status_code == 400


async def test_instantiate_department_unknown_plugin_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/capas/{uuid.uuid4()}/instantiate-department",
                json={"name": "X"},
                headers=_headers(tenant),
            )
            assert r.status_code == 404
