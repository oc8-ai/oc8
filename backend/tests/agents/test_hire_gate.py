from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agents.hire import set_require_hire_approval
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _NeverRunsRuntime:
    """A RuntimeAdapter stub that fails the test if the executor ever invokes
    it -- proves the pending_approval guard short-circuits BEFORE dispatch,
    which is what makes the gate hold across every enqueue path (delegation,
    cron/triggers), not only the HTTP /run endpoint."""

    async def execute(self, db: Any, **kw: Any) -> Any:
        raise AssertionError("runtime must not be invoked for a pending_approval agent")


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _dept(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        s.add(dept)
        await s.flush()
        return dept.id


async def _enable_gate(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    async with app_session(tenant) as s:
        s.add(m.Organization(id=tenant, slug=f"t{tenant.hex[:6]}", name="T",
                             tier="standard", region="eu", settings={}))
        await s.flush()
        await set_require_hire_approval(s, tenant_id=tenant, enabled=True)


def _create_body(dept_id: uuid.UUID) -> dict[str, str]:
    return {"name": "Rep", "departmentId": str(dept_id)}


async def test_gate_off_creates_a_ready_agent_no_incident(app_session: AppSessionFactory) -> None:
    """E2E N9: a fresh hire showed "paused" -- nobody paused it. It is ready
    for work; only a deliberate pause or stop shows as one."""
    tenant = uuid.uuid4()
    dept_id = await _dept(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                "/api/v1/agents", json=_create_body(dept_id), headers=_headers(tenant)
            )
            assert r.status_code == 201
            assert r.json()["status"] == "waiting_for_task"
            assert r.json()["awaitingHireApproval"] is False
            agent_id = r.json()["id"]
    async with app_session(tenant) as s:
        # r.json()["status"] is the UI-facing 4-state mapping (agent_to_dto /
        # AGENT_STATUS_TO_UI), not the raw lifecycle status -- assert on the row.
        agent_row = await s.get(m.Agent, uuid.UUID(agent_id))
        assert agent_row is not None and agent_row.status == "idle"
        incidents = (await s.execute(
            select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "hire_agent")
        )).scalars().all()
        assert incidents == []


async def test_gate_on_creates_pending_with_incident_and_blocks(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    dept_id = await _dept(app_session, tenant)
    await _enable_gate(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                "/api/v1/agents", json=_create_body(dept_id), headers=_headers(tenant)
            )
            assert r.status_code == 201
            agent_id = r.json()["id"]
            # The DTO surfaces a gated agent as "warning" (needs attention), NOT
            # "running" -- pending_approval maps through AGENT_STATUS_TO_UI.
            assert r.json()["status"] == "warning"
            # Said plainly, so the wizard publishes nothing on top of it.
            assert r.json()["awaitingHireApproval"] is True
            # a pending agent cannot run or be started
            run = await client.post(f"/api/v1/agents/{agent_id}/run",
                                    json={"task": "x"}, headers=_headers(tenant))
            assert run.status_code == 409
            life = await client.post(f"/api/v1/agents/{agent_id}/lifecycle",
                                     json={"action": "start"}, headers=_headers(tenant))
            assert life.status_code == 409
            # Nor can a change be published on top of what the approver sees:
            # they approve what was requested (review of round 6).
            published = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1}, headers=_headers(tenant),
            )
            assert published.status_code == 409, published.text
            assert "hire approval" in published.text

    async with app_session(tenant) as s:
        agent_row = await s.get(m.Agent, uuid.UUID(agent_id))
        assert agent_row is not None and agent_row.status == "pending_approval"
        incidents = (await s.execute(
            select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "hire_agent")
        )).scalars().all()
        assert len(incidents) == 1 and incidents[0].payload["name"] == "Rep"


async def test_executor_blocks_a_pending_approval_agent_run(
    app_session: AppSessionFactory,
) -> None:
    """The gate must hold at the executor -- the single choke point every run
    source (HTTP /run, delegate_task, cron/triggers) funnels through -- not
    only at the HTTP endpoint. A run enqueued directly for a pending_approval
    agent (as delegation or a trigger would) must end FAILED without the
    runtime ever being invoked."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Unhired",
            status="pending_approval",
        )
        s.add(agent)
        await s.flush()
        repo = RunRepository(s)
        run = await repo.create(tenant_id=tenant, agent_id=agent.id, context={"task": "x"})
        run_id = run.id

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_NeverRunsRuntime(),
    )

    async with app_session(tenant) as s:
        refreshed = await s.get(m.AgentRun, run_id)
        assert refreshed is not None
        assert refreshed.state == RunState.FAILED.value
        assert "awaiting hire approval" in refreshed.context["error"]
