"""Archiving flows and cancelling flow runs (§14a.4).

A flow could be published but never retired, so it stayed startable forever,
and a run could not be stopped once its handoffs were out.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.collab.handoff import create_handoff_type
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

TENANT = uuid.UUID(str(ACME_TENANT_ID))


def _headers() -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=TENANT, subject="u", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _spec(flow_id: str, first: str, second: str) -> dict[str, object]:
    to_dept = str(uuid.uuid4())
    return {
        "id": flow_id,
        "version": "1.0.0",
        "trigger": {"event": "x.happened", "from_department_id": str(uuid.uuid4())},
        "stages": [
            {"id": "kickoff", "handoff": {"type": first, "to_department_id": to_dept}},
            {
                "id": "build",
                "after": "kickoff.completed",
                "handoff": {"type": second, "to_department_id": to_dept},
            },
        ],
    }


async def _published_flow(app_session: AppSessionFactory, client: AsyncClient) -> tuple[str, str]:
    first, second = f"ht.{_suffix()}", f"ht.{_suffix()}"
    async with app_session(TENANT) as db:
        for name in (first, second):
            await create_handoff_type(
                db, tenant_id=TENANT, name=name, payload_schema={"type": "object"}
            )
    pub = await client.post(
        "/api/v1/flows",
        json={"spec": _spec(f"flow-{_suffix()}", first, second)},
        headers=_headers(),
    )
    assert pub.status_code == 201
    return str(pub.json()["flowId"]), str(pub.json()["id"])


async def test_an_archived_flow_is_hidden_and_cannot_start(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    flow_id, version_id = await _published_flow(app_session, client)
    started = await client.post(
        f"/api/v1/flow-versions/{version_id}/start", json={}, headers=_headers()
    )
    run_id = started.json()["id"]

    r = await client.post(f"/api/v1/flows/{flow_id}/archive", headers=_headers())
    assert r.status_code == 200

    listed = await client.get("/api/v1/flows", headers=_headers())
    assert all(f["id"] != flow_id for f in listed.json())

    refused = await client.post(
        f"/api/v1/flow-versions/{version_id}/start", json={}, headers=_headers()
    )
    assert refused.status_code == 409

    # The run already going is left alone.
    run = await client.get(f"/api/v1/flow-runs/{run_id}", headers=_headers())
    assert run.json()["status"] == "running"

    missing = await client.post(f"/api/v1/flows/{uuid.uuid4()}/archive", headers=_headers())
    assert missing.status_code == 404


async def test_cancelling_a_flow_run_stops_its_open_steps(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    _, version_id = await _published_flow(app_session, client)
    started = await client.post(
        f"/api/v1/flow-versions/{version_id}/start", json={}, headers=_headers()
    )
    run_id = uuid.UUID(started.json()["id"])

    # The kickoff handoff was accepted and its work is underway on a task.
    async with app_session(TENANT) as db:
        handoff = (
            await db.execute(select(m.Handoff).where(m.Handoff.flow_run_id == run_id))
        ).scalar_one()
        dept = m.Department(tenant_id=TENANT, name=f"work-{_suffix()}", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=TENANT, department_id=dept.id, name="Lead", definition={})
        db.add(agent)
        await db.flush()
        task = m.Task(
            tenant_id=TENANT, department_id=dept.id, title="Übergabe", state="in_progress"
        )
        db.add(task)
        await db.flush()
        agent_run = m.AgentRun(
            tenant_id=TENANT, agent_id=agent.id, task_id=task.id, context={}, state="queued"
        )
        db.add(agent_run)
        handoff.status = "accepted"
        handoff.target_task_id = task.id
        await db.flush()
        handoff_id, agent_run_id = handoff.id, agent_run.id

    r = await client.post(f"/api/v1/flow-runs/{run_id}/cancel", headers=_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    async with app_session(TENANT) as db:
        h = await db.get(m.Handoff, handoff_id)
        assert h is not None and h.status == "rejected"
        cancel = (
            await db.execute(
                select(m.RunCancellation).where(m.RunCancellation.run_id == agent_run_id)
            )
        ).scalar_one_or_none()
        assert cancel is not None

    again = await client.post(f"/api/v1/flow-runs/{run_id}/cancel", headers=_headers())
    assert again.status_code == 409

    missing = await client.post(f"/api/v1/flow-runs/{uuid.uuid4()}/cancel", headers=_headers())
    assert missing.status_code == 404
