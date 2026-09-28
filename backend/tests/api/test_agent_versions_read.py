"""`GET /agents/{id}/versions`.

Gated `require_departmental(perm(AGENT_VERSION, VIEW))` -- the gate shape the
read side of `agents.py` already uses -- so the caller's `HumanActor` is
resolved once and `visible_agent` can apply `scope.viewable`. A caller who can
see the agent but does not hold the new permission gets a 403, and one who
holds the permission but cannot see the agent gets a 404 that is byte-identical
to a genuinely missing id.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def _agent_with_versions(
    app_session: AppSessionFactory, tenant: uuid.UUID, count: int
) -> uuid.UUID:
    """An agent this test owns, with exactly `count` versions.

    Its own department and its own agent every time: ACME_TENANT_ID is shared
    across the suite with no per-test rollback, so any assertion about "how
    many versions" has to be scoped to a row the test created.
    """
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        db.add(department)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name=f"A-{uuid.uuid4().hex[:8]}",
            mission="m0",
        )
        db.add(agent)
        await db.flush()
        for n in range(count):
            agent.mission = f"m{n}"
            await db.flush()
            await publish_version(db, agent, note=f"note-{n}")
        return agent.id


async def test_versions_come_back_newest_first_with_the_current_one_marked(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_with_versions(app_session, tenant, 3)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["totalCount"] == 3
    assert [row["versionNo"] for row in body["items"]] == [3, 2, 1]
    assert [row["isCurrent"] for row in body["items"]] == [True, False, False]
    assert body["items"][0]["note"] == "note-2"
    # The list is a LIST: shipping every payload to render ten rows is what the
    # separate single-version route exists to avoid.
    assert "payload" not in body["items"][0]


async def test_the_list_is_paged(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_with_versions(app_session, tenant, 5)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            first = await client.get(f"/api/v1/agents/{agent_id}/versions?limit=2", headers=headers)
            second = await client.get(
                f"/api/v1/agents/{agent_id}/versions?limit=2&offset=2", headers=headers
            )
    assert [r["versionNo"] for r in first.json()["items"]] == [5, 4]
    assert [r["versionNo"] for r in second.json()["items"]] == [3, 2]
    assert first.json()["totalCount"] == 5


async def test_a_caller_without_the_permission_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """`member` holds `copilot:use` and nothing else, so it holds neither
    `agent:view` nor `agent_version:view`."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_with_versions(app_session, tenant, 1)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions",
                headers={"Authorization": f"Bearer {_token(tenant, 'member')}"},
            )
    assert res.status_code == 403, res.text


async def test_an_unknown_agent_is_a_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{uuid.uuid4()}/versions",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 404, res.text


async def test_versions_of_another_agent_never_leak_into_the_list(
    app_session: AppSessionFactory,
) -> None:
    """`agent_version` has no foreign key (house convention), so the
    `agent_id == agent.id` term is the ONLY thing scoping this query. A missing
    one would return the tenant's whole version history on every request."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    mine = await _agent_with_versions(app_session, tenant, 2)
    await _agent_with_versions(app_session, tenant, 4)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{mine}/versions",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.json()["totalCount"] == 2
