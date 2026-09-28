"""`GET /agents/{id}/versions/diff?from=&to=`.

Two things are worth a test each beyond the happy path: that a bare `from`
diffs against the WORKING COPY rather than against the current version (that
is the Review button's whole job), and that the literal path segment `diff`
does not get captured by `/{version_no}`'s integer converter -- a route
ordering bug that would make this endpoint return 422 forever while every unit
test of `diff_payloads` stayed green.
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


async def _agent_v1_v2(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    """v1 with mission "first", v2 with mission "second", working copy "third"."""
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        db.add(department)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name=f"A-{uuid.uuid4().hex[:8]}",
            mission="first",
        )
        db.add(agent)
        await db.flush()
        await publish_version(db, agent, note="v1")
        agent.mission = "second"
        await db.flush()
        await publish_version(db, agent, note="v2")
        agent.mission = "third"
        await db.flush()
        return agent.id


async def test_diff_between_two_published_versions(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_v1_v2(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions/diff?from=1&to=2",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["fromVersionNo"] == 1
    assert body["toVersionNo"] == 2
    assert body["entries"] == [{"field": "mission", "before": "first", "after": "second"}]


async def test_omitting_to_diffs_against_the_working_copy(
    app_session: AppSessionFactory,
) -> None:
    """The Review button. `toVersionNo` is null rather than the current number:
    the right-hand side is the unpublished draft, and labelling it "v2" would
    claim this was a comparison of two published versions."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_v1_v2(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions/diff?from=2",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["toVersionNo"] is None
    assert body["entries"] == [{"field": "mission", "before": "second", "after": "third"}]


async def test_the_literal_diff_segment_is_not_read_as_a_version_number(
    app_session: AppSessionFactory,
) -> None:
    """Route-ordering regression. `/versions/{version_no}` declares an int path
    param; if it is registered first, `/versions/diff` matches it and FastAPI
    answers 422 before this handler is ever reached."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_v1_v2(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions/diff?from=1&to=2",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code != 422, res.text


async def test_identical_versions_diff_to_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_v1_v2(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions/diff?from=1&to=1",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 200, res.text
    assert res.json()["entries"] == []


async def test_an_unknown_from_version_is_a_404(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_v1_v2(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions/diff?from=99",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 404, res.text


async def test_diffing_needs_the_permission(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id = await _agent_v1_v2(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/agents/{agent_id}/versions/diff?from=1&to=2",
                headers={"Authorization": f"Bearer {_token(tenant, 'member')}"},
            )
    assert res.status_code == 403, res.text
