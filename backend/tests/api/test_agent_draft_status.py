"""`GET /agents/{id}/draft-status`.

The publish bar's data source. Four behaviours matter and each is a decision
somebody could reverse without noticing: a fresh agent is CLEAN (because
`create_agent` publishes v1 in the same transaction), a rename never dirties it
(decision 5 -- `name` is not versioned), a mission edit does, and the field
list is granular enough to be worth rendering.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def _hire(client: AsyncClient, headers: dict[str, str]) -> str:
    """Create an agent through the real endpoint, so v1 is published exactly the
    way production publishes it -- a hand-built row would not have a current
    version and would test the wrong branch."""
    dept = await client.post(
        "/api/v1/departments",
        json={"name": f"D-{uuid.uuid4().hex}", "icon": "Bot"},
        headers=headers,
    )
    assert dept.status_code in (200, 201), dept.text
    created = await client.post(
        "/api/v1/agents",
        json={
            "departmentId": dept.json()["id"],
            "name": f"A-{uuid.uuid4().hex[:8]}",
            "roleTitle": "Tester",
            "mission": "original",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_a_freshly_hired_agent_is_not_dirty(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            res = await client.get(f"/api/v1/agents/{agent_id}/draft-status", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dirty"] is False
    assert body["changedFields"] == []
    assert body["currentVersionNo"] == 1


async def test_editing_the_instructions_makes_it_dirty(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            patched = await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "changed"},
                headers=headers,
            )
            assert patched.status_code == 200, patched.text
            res = await client.get(f"/api/v1/agents/{agent_id}/draft-status", headers=headers)
    body = res.json()
    assert body["dirty"] is True
    assert body["changedFields"] == ["mission"]
    assert body["currentVersionNo"] == 1


async def test_renaming_never_makes_it_dirty(app_session: AppSessionFactory) -> None:
    """Decision 5, end to end: a rename must not light up a publish bar, burn a
    version number, or trigger a compliance re-check. `PATCH /agents/{id}/name`
    is the real writer, so this goes through it rather than assigning a column."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            renamed = await client.patch(
                f"/api/v1/agents/{agent_id}/name",
                json={"name": f"Renamed-{uuid.uuid4().hex[:8]}"},
                headers=headers,
            )
            assert renamed.status_code == 200, renamed.text
            res = await client.get(f"/api/v1/agents/{agent_id}/draft-status", headers=headers)
    assert res.json()["dirty"] is False


async def test_draft_status_needs_the_permission(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            agent_id = await _hire(client, {"Authorization": f"Bearer {_token(tenant)}"})
            res = await client.get(
                f"/api/v1/agents/{agent_id}/draft-status",
                headers={"Authorization": f"Bearer {_token(tenant, 'member')}"},
            )
    assert res.status_code == 403, res.text


async def test_draft_status_of_an_unknown_agent_is_a_404(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{uuid.uuid4()}/draft-status",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 404, res.text
