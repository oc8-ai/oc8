"""`GET /runs/{id}` names the version the run is pinned to.

The point of pinning is that a transcript can be read against the
configuration that produced it (spec §5). A run that pins a version and never
says which one keeps that question unanswerable from the UI, which is most of
the value of having done the pinning at all.

`null` for a historical run is the correct answer, not a bug: migration 0098
deliberately did NOT backfill `agent_run.agent_version_id`, because inventing a
version for a run that happened before versioning existed would be a claim
about the past.

Two routes serialize a `RunDTO` for the exact same frontend query-cache entry
(`["run", runId]`): the run-detail route here, and the chat surface's
`GET /chat/sessions/{sessionId}/runs/{runId}` (used by `useCopilotRunActivity`).
Both must fill `agentVersionNo` identically, or whichever query refetches last
silently overwrites the other's value with null.
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


async def test_a_pinned_run_reports_its_version_number(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        db.add(department)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name=f"A-{uuid.uuid4().hex[:8]}",
            mission="m",
        )
        db.add(agent)
        await db.flush()
        await publish_version(db, agent)
        agent.mission = "m2"
        await db.flush()
        v2 = await publish_version(db, agent)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="done",
            agent_version_id=v2.id,
        )
        db.add(run)
        await db.flush()
        run_id = run.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/runs/{run_id}",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 200, res.text
    assert res.json()["agentVersionNo"] == 2


async def test_a_historical_run_reports_null_rather_than_guessing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        db.add(department)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name=f"A-{uuid.uuid4().hex[:8]}",
            mission="m",
        )
        db.add(agent)
        await db.flush()
        await publish_version(db, agent)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="done", agent_version_id=None)
        db.add(run)
        await db.flush()
        run_id = run.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            res = await client.get(
                f"/api/v1/runs/{run_id}",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert res.status_code == 200, res.text
    assert res.json()["agentVersionNo"] is None


async def test_the_agent_detail_names_its_current_version(
    app_session: AppSessionFactory,
) -> None:
    """The thin shape `lib/skills.ts` established: an id plus the number, and no
    client-side version state machine."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            dept = await client.post(
                "/api/v1/departments",
                json={"name": f"D-{uuid.uuid4().hex}", "icon": "Bot"},
                headers=headers,
            )
            created = await client.post(
                "/api/v1/agents",
                json={
                    "departmentId": dept.json()["id"],
                    "name": f"A-{uuid.uuid4().hex[:8]}",
                    "roleTitle": "Tester",
                    "mission": "m",
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["currentVersionNo"] == 1
            assert body["currentVersionId"]
            uuid.UUID(body["currentVersionId"])


async def test_the_chat_run_detail_route_fills_the_same_field(
    app_session: AppSessionFactory,
) -> None:
    """Ruling C7: `GET /chat/sessions/{sessionId}/runs/{runId}` seeds the exact
    same frontend query-cache entry (`["run", runId]`) as `GET /runs/{id}`. If
    only one of the two routes filled `agentVersionNo`, the version badge would
    flicker in and out for no reason a user could explain, depending on which
    query last wrote the cache. Both must report the same number for the same
    pinned run.
    """
    # A fresh tenant, not ACME_TENANT_ID: the session must be owned by the
    # SAME member the token resolves to (chat.py's `_owned_session`), which is
    # simplest to get right by creating the session through the real API
    # rather than inserting a `ChatSession` row with a guessed `member_id`.
    tenant = uuid.uuid4()
    headers = {"Authorization": f"Bearer {_token(tenant)}"}
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        db.add(department)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name=f"A-{uuid.uuid4().hex[:8]}",
            mission="m",
        )
        db.add(agent)
        await db.flush()
        await publish_version(db, agent)
        agent.mission = "m2"
        await db.flush()
        v2 = await publish_version(db, agent)
        agent_id = agent.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            create_r = await client.post(
                "/api/v1/chat/sessions",
                json={"agentId": str(agent_id)},
                headers=headers,
            )
            assert create_r.status_code == 201, create_r.text
            session_id = create_r.json()["id"]

            async with app_session(tenant) as db:
                run = m.AgentRun(
                    tenant_id=tenant,
                    agent_id=agent_id,
                    state="done",
                    agent_version_id=v2.id,
                    context={"chat_session_id": session_id},
                )
                db.add(run)
                await db.flush()
                run_id = run.id

            res = await client.get(
                f"/api/v1/chat/sessions/{session_id}/runs/{run_id}",
                headers=headers,
            )
    assert res.status_code == 200, res.text
    assert res.json()["agentVersionNo"] == 2
