"""PUT /agents/{id}/autonomy -- the deliberate switch that lets an agent act
without asking (E2E F2).

`b_approval.posture` lets an outward call (send a mail, post a message) run
unasked only for an agent whose definition says `autonomy: "autonomous"` AND
grants the `outward` tier. Nothing could set either, so every send of an
"autonomous" guardrail preset still waited for a person."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.harness.stages.b_approval import autonomy_of, posture
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _agent(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Support", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            definition={"oc8_agent": 1, "max_steps": 12},
        )
        db.add(agent)
        await db.flush()
        return agent.id


async def test_an_admin_makes_an_agent_autonomous_for_outward_calls(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    async with _http() as c:
        before = await c.get(f"/api/v1/agents/{agent_id}", headers=_h(tenant))
        assert before.json()["autonomy"] == "default"
        assert before.json()["autonomyGrants"] == []
        r = await c.put(
            f"/api/v1/agents/{agent_id}/autonomy",
            json={"autonomy": "autonomous", "grants": ["outward"]},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        assert r.json()["autonomy"] == "autonomous"
        assert r.json()["autonomyGrants"] == ["outward"]

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        definition = agent.definition
        # Other definition keys survive.
        assert definition["max_steps"] == 12
        # A deliberate choice leaves a trace: who, what, from what.
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "agent.autonomy.updated")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].resource.get("agent_id") == str(agent_id)
    assert posture(
        "outward", autonomy_of(definition), granted="outward" in definition["b5_grants"]
    ) == ("allow")


async def test_leaving_autonomous_drops_the_grants(app_session: AppSessionFactory) -> None:
    """A grant only means anything under `autonomous`; kept under another
    level it would silently come back the day someone picks autonomous again."""
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    async with _http() as c:
        await c.put(
            f"/api/v1/agents/{agent_id}/autonomy",
            json={"autonomy": "autonomous", "grants": ["outward"]},
            headers=_h(tenant),
        )
        r = await c.put(
            f"/api/v1/agents/{agent_id}/autonomy",
            json={"autonomy": "default", "grants": ["outward"]},
            headers=_h(tenant),
        )
        assert r.status_code == 200, r.text
        assert r.json()["autonomyGrants"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"autonomy": "reckless", "grants": []},
        {"autonomy": "autonomous", "grants": ["read"]},
        {"autonomy": "autonomous", "grants": ["everything"]},
    ],
)
async def test_unknown_levels_and_tiers_are_refused(
    app_session: AppSessionFactory, body: dict[str, object]
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    async with _http() as c:
        r = await c.put(f"/api/v1/agents/{agent_id}/autonomy", json=body, headers=_h(tenant))
        assert r.status_code == 422, r.text


async def test_a_member_without_agent_write_may_not(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    async with _http() as c:
        r = await c.put(
            f"/api/v1/agents/{agent_id}/autonomy",
            json={"autonomy": "autonomous", "grants": ["outward"]},
            headers=_h(tenant, role="member"),
        )
        # A tenant-wide `member` holds no agent write at all: refused at the
        # door, not answered as a missing agent.
        assert r.status_code == 403, r.text
