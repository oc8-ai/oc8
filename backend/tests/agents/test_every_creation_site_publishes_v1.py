"""Ruling C4: every production path that creates an agent publishes its v1 in
the same transaction, so `Agent.current_version_id` is never null for an agent
a run can be started for. `create_agent` (the HTTP route) is covered by its own
tests; these are the other three: capa template hire, department template,
and the lazily-provisioned tenant Assistant."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.capas.service import install_plugin, instantiate_agent, instantiate_department
from tests.conftest import AppSessionFactory


async def _v1(db: AsyncSession, agent: m.Agent) -> m.AgentVersion:
    assert agent.current_version_id is not None, f"{agent.name} has no published version"
    version = await db.get(m.AgentVersion, agent.current_version_id)
    assert version is not None
    assert version.version_no == 1
    assert version.agent_id == agent.id
    return version


async def test_instantiate_agent_publishes_v1(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    manifest: dict[str, object] = {
        "name": "finance_bookkeeper",
        "version": "1.0.0",
        "type": "agent_template",
        "trust": "first_party",
        "summary": "Bookkeeping.",
        "agent_template": {"name": "Dana", "mission": "Keep the books accurate."},
    }
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        db.add(dept)
        await db.flush()
        version = await install_plugin(db, tenant_id=tenant, manifest_data=manifest)
        agent = await instantiate_agent(
            db, tenant_id=tenant, version=version, department_id=dept.id
        )
        v1 = await _v1(db, agent)
        assert v1.payload["mission"] == "Keep the books accurate."


async def test_instantiate_department_publishes_v1_for_every_team_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    version = m.CapaVersion(
        tenant_id=tenant,
        capa_id=uuid.uuid4(),
        semver="1.0.0",
        manifest={
            "name": "sales",
            "version": "1.0.0",
            "type": "department_template",
            "department_template": {
                "frame": {"tools": {}},
                "agents": [
                    {"name": "Lead", "mission": "lead", "is_team_lead": True},
                    {"name": "Rep", "mission": "sell", "reports_to": "Lead"},
                ],
            },
        },
        artifact_hash=b"x",
        permissions=[],
        capabilities=[],
        entry_points={},
    )
    async with app_session(tenant) as db:
        dept = await instantiate_department(db, tenant_id=tenant, version=version, name="Sales")
        agents = (
            (await db.execute(select(m.Agent).where(m.Agent.department_id == dept.id)))
            .scalars()
            .all()
        )
        assert len(agents) == 2
        for agent in agents:
            v1 = await _v1(db, agent)
            assert v1.payload["mission"] == agent.mission


async def test_the_tenant_assistant_is_created_with_v1(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await get_or_create_assistant(db, tenant_id=tenant)
        v1 = await _v1(db, agent)
        assert v1.payload["is_team_lead"] is True
        # A second call finds the existing Assistant and publishes nothing new.
        again = await get_or_create_assistant(db, tenant_id=tenant)
        assert again.current_version_id == v1.id


async def test_the_assistants_model_follow_publishes_a_new_version(
    app_session: AppSessionFactory,
) -> None:
    """Runs execute the pinned version, so the Assistant's automatic
    model-follow (`_sync_model_config`) must publish -- otherwise the switch
    lands on the row and never reaches a run."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first = m.ModelConfig(
            tenant_id=tenant, provider="anthropic", model="a", used_by_copilot=True
        )
        second = m.ModelConfig(
            tenant_id=tenant, provider="anthropic", model="b", used_by_copilot=False
        )
        db.add_all([first, second])
        await db.flush()
        agent = await get_or_create_assistant(db, tenant_id=tenant)
        v1 = await _v1(db, agent)

        first.used_by_copilot = False
        second.used_by_copilot = True
        await db.flush()
        again = await get_or_create_assistant(db, tenant_id=tenant)

        assert again.current_version_id != v1.id
        v2 = await db.get(m.AgentVersion, again.current_version_id)
        assert v2 is not None and v2.version_no == 2
        assert v2.payload["model_config_id"] == str(second.id)


async def test_a_copilot_agent_create_publishes_v1(app_session: AppSessionFactory) -> None:
    """Not named in ruling C4, found by sweeping every `m.Agent(` in src: the
    Copilot's `agent.create` operation is a fifth creation path."""
    from oc8.auth import Principal
    from oc8.copilot.proposals import apply_proposal, create_proposal

    tenant = uuid.uuid4()
    actor = Principal(subject="operator-1", tenant_id=tenant, role="org_admin")
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.create",
                    "departmentId": str(dept.id),
                    "name": "Nora",
                    "roleTitle": "SDR",
                    "mission": "Qualify inbound leads",
                }
            ],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        agent = (
            await db.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        v1 = await _v1(db, agent)
        assert v1.payload["mission"] == "Qualify inbound leads"
