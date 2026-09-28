from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append({"run_id": run_id, "tenant_id": tenant_id})


async def test_run_enqueues_and_status_polls(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        agent_id = agent.id

    app = create_app()
    fake = _FakeQueue()
    # enqueue_run (the single intake path, §14.1) resolves the queue itself
    # via oc8.runtime.intake.get_run_queue -- there's no more per-request
    # FastAPI-injected queue to override, so isolate it the same way
    # tests/runtime/test_intake.py does.
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: fake)
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {token}"}
            r = await client.post(
                f"/api/v1/agents/{agent_id}/run",
                json={"task": "build it"},
                headers=headers,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["state"] == "queued"
            run_id = body["id"]
            assert len(fake.enqueued) == 1
            assert fake.enqueued[0]["run_id"] == uuid.UUID(run_id)
            assert fake.enqueued[0]["tenant_id"] == tenant

            g = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
            assert g.status_code == 200
            assert g.json()["state"] == "queued"

            # Fix 1 regression guard: the row must be durably committed (not just
            # flushed) before enqueue, so it is visible from a brand-new session
            # even though this session's own transaction hasn't ended yet.
            async with app_session(tenant) as s:
                run_row = await s.get(m.AgentRun, uuid.UUID(run_id))
                assert run_row is not None
                assert run_row.state == "queued"


async def test_run_todos_reach_the_wire(app_session: AppSessionFactory) -> None:
    """The todo_write control tool's list is stored on
    `AgentRun.context["todos"]` and must round-trip through `GET /runs/{id}`
    -- same durable-field shape as rendered_components (see test_chat.py's
    equivalent regression test for that field's snake/camel aliasing)."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="done",
            context={"todos": [{"content": "Check the invoice", "status": "in_progress"}]},
        )
        s.add(run)
        await s.flush()
        run_id = run.id

    app = create_app()
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {token}"}
            r = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["todos"] == [{"content": "Check the invoice", "status": "in_progress"}]


async def test_run_updated_at_reaches_the_wire(app_session: AppSessionFactory) -> None:
    """`AgentRun.updated_at` is the run's heartbeat (runtime/reconcile.py's
    HEARTBEAT_SECONDS/ABANDONED_AFTER) and must round-trip through
    `GET /runs/{id}` as `updatedAt` -- the frontend's staleness indicator on
    the agent detail page has nothing to compare "now" against otherwise."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
        s.add(run)
        await s.flush()
        run_id = run.id
        expected_updated_at = run.updated_at

    app = create_app()
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {token}"}
            r = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert "updatedAt" in body
            assert body["updatedAt"] == expected_updated_at.isoformat()
