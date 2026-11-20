"""`POST /agents` publishes v1 in the same transaction as the create (A2 of
the versioning spec): every hired agent has a `current_version_id` from the
moment it exists, matching the invariant the 0099 backfill establishes for
pre-existing rows."""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID) -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role="org_admin")


def _h(tenant: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(tenant)}"}


async def test_create_agent_publishes_v1_with_the_hired_mission(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        dept_id = dept.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            resp = await client.post(
                "/api/v1/agents",
                json={
                    "name": "Versioned Agent",
                    "departmentId": str(dept_id),
                    "roleTitle": "Tester",
                    "mission": "answer support tickets",
                },
                headers=_h(tenant),
            )
            assert resp.status_code == 201, resp.text
            agent_id = uuid.UUID(resp.json()["id"])

    async with app_session(tenant) as db:
        a = await db.get(m.Agent, agent_id)
        assert a is not None
        assert a.current_version_id is not None

        version = await db.get(m.AgentVersion, a.current_version_id)
        assert version is not None
        assert version.version_no == 1
        assert version.payload["mission"] == "answer support tickets"

        published_events = (
            (
                await db.execute(
                    select(m.AuditEvent.action).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "agent.version.published",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert published_events == ["agent.version.published"]
