"""agent.runtime.assign / agent.model.switch / agent.skill.assign."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def _agent(db, tenant):
    dept = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add(dept)
    await db.flush()
    # A name distinct from other copilot tests' "Nora" -- `app_session` commits,
    # and several other files in this same session-scoped `acme_tenant` look up
    # an agent by `name == "Nora"`; reusing that name here breaks those lookups
    # with `MultipleResultsFound` (see `test_agent_narrowing_operations.py`'s
    # identical note).
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Talia", definition={})
    db.add(agent)
    await db.flush()
    return agent


async def test_runtime_assign_clears_with_an_explicit_null(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "agent.runtime.assign", "agentId": str(agent.id), "runtimePluginId": None}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"


async def test_runtime_assign_rejects_an_unknown_runtime(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.runtime.assign",
                    "agentId": str(agent.id),
                    "runtimePluginId": "does-not-exist",
                }
            ],
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_model_switch_rejects_an_unknown_model_config(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.model.switch",
                    "agentId": str(agent.id),
                    "modelConfigId": str(uuid.uuid4()),
                }
            ],
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_model_switch_applies_a_known_model_config(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        mc = m.ModelConfig(tenant_id=acme_tenant, provider="anthropic", model="claude-sonnet-5")
        db.add(mc)
        await db.flush()
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "agent.model.switch", "agentId": str(agent.id), "modelConfigId": str(mc.id)}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        await db.refresh(agent)
        assert agent.model_config_id == mc.id


async def test_skill_assign_is_a_no_op_when_the_agent_already_has_the_skill_enabled(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        skill = m.Skill(tenant_id=acme_tenant, name="S", origin="local", trust_level="first_party")
        db.add(skill)
        await db.flush()
        version = m.SkillVersion(
            tenant_id=acme_tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={"requires": {"tools": [], "kbs": []}},
            artifact_hash=b"x" * 32,
        )
        db.add(version)
        await db.flush()
        db.add(
            m.SkillAssignment(
                tenant_id=acme_tenant, agent_id=agent.id, skill_version_id=version.id, enabled=True
            )
        )
        await db.flush()

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.skill.assign",
                    "agentId": str(agent.id),
                    "skillVersionId": str(version.id),
                }
            ],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        rows = (
            (
                await db.execute(
                    select(m.SkillAssignment).where(m.SkillAssignment.agent_id == agent.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "must not have inserted a duplicate row"


async def test_skill_assign_rejects_an_unknown_skill_version(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.skill.assign",
                    "agentId": str(agent.id),
                    "skillVersionId": str(uuid.uuid4()),
                }
            ],
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)
