"""agent.rename / agent.lifecycle.set / agent.delete / agent.restore."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.proposals import apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def _agent(db, tenant):
    dept = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add(dept)
    await db.flush()
    agent_name = f"TestAgent-{uuid.uuid4().hex[:8]}"
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name=agent_name, definition={})
    db.add(agent)
    await db.flush()
    return agent


async def test_agent_rename_applies(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        proposal = await create_proposal(
            db, actor, [{"type": "agent.rename", "agentId": str(agent.id), "name": "Nora B."}]
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        await db.refresh(agent)
        assert agent.name == "Nora B."


async def test_agent_lifecycle_set_starts_a_stopped_agent(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        agent.status = "stopped"
        await db.flush()
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "agent.lifecycle.set", "agentId": str(agent.id), "action": "start"}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        await db.refresh(agent)
        assert agent.status == "running"


async def test_agent_delete_archives_when_it_has_run_history(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        db.add(m.AgentRun(tenant_id=acme_tenant, agent_id=agent.id, state="done", context={}))
        await db.flush()
        proposal = await create_proposal(
            db, actor, [{"type": "agent.delete", "agentId": str(agent.id)}]
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        await db.refresh(agent)
        assert agent.deleted_at is not None


async def test_agent_restore_clears_deleted_at(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        import datetime as dt

        agent = await _agent(db, acme_tenant)
        agent.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        proposal = await create_proposal(
            db, actor, [{"type": "agent.restore", "agentId": str(agent.id)}]
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        await db.refresh(agent)
        assert agent.deleted_at is None
