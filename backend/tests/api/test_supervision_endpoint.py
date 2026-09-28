from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(tenant, role)}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            yield client


async def test_policy_assignment_anchor_flow() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    supervised = str(uuid.uuid4())

    async with _http() as client:
        pol = await client.post(
            "/api/v1/supervision/policies",
            json={
                "departmentId": str(uuid.uuid4()),
                "checkpointEvery": {"toolCalls": 5},
                "driftThresholds": {"steer": 0.35, "rewind": 0.55, "escalate": 0.75},
                "allowedInterventions": ["steer", "rewind"],
            },
            headers=_headers(tenant),
        )
        assert pol.status_code == 201
        policy_id = pol.json()["id"]

        asg = await client.post(
            "/api/v1/supervision/assignments",
            json={
                "supervisorAgentId": str(uuid.uuid4()),
                "supervisedAgentId": supervised,
                "policyId": policy_id,
            },
            headers=_headers(tenant),
        )
        assert asg.status_code == 201

        dup = await client.post(
            "/api/v1/supervision/assignments",
            json={
                "supervisorAgentId": str(uuid.uuid4()),
                "supervisedAgentId": supervised,
                "policyId": policy_id,
            },
            headers=_headers(tenant),
        )
        assert dup.status_code == 409

        anc = await client.post(
            "/api/v1/task-anchors",
            json={
                "taskId": str(uuid.uuid4()),
                "objective": "Ship the Odoo module",
                "acceptanceCriteria": {"tests": "green"},
                "constraints": {"budgetEur": 1000},
            },
            headers=_headers(tenant),
        )
        assert anc.status_code == 201
        assert anc.json()["objective"] == "Ship the Odoo module"


async def test_agent_supervisor_upsert_and_clear(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        supervised = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="worker-sup")
        supervisor = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="head-sup")
        db.add_all([supervised, supervisor])
        await db.flush()
        supervised_id, supervisor_id = str(supervised.id), str(supervisor.id)

    headers = _headers(tenant)
    async with _http() as client:
        r0 = await client.get(f"/api/v1/agents/{supervised_id}/supervisor", headers=headers)
        assert r0.status_code == 200 and r0.json()["driftGuard"] is False

        r1 = await client.put(
            f"/api/v1/agents/{supervised_id}/supervisor",
            json={"supervisorAgentId": supervisor_id, "driftGuard": True},
            headers=headers,
        )
        assert r1.status_code == 200
        assert r1.json()["driftGuard"] is True
        assert r1.json()["supervisorAgentId"] == supervisor_id
        assert r1.json()["policyId"]

        r2 = await client.get(f"/api/v1/agents/{supervised_id}/supervisor", headers=headers)
        assert r2.json()["supervisorAgentId"] == supervisor_id

        r3 = await client.put(
            f"/api/v1/agents/{supervised_id}/supervisor",
            json={"driftGuard": False},
            headers=headers,
        )
        assert r3.status_code == 200 and r3.json()["driftGuard"] is False
        r4 = await client.get(f"/api/v1/agents/{supervised_id}/supervisor", headers=headers)
        assert r4.json()["driftGuard"] is False
