from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agents.versioning import draft_status
from oc8.capas.service import PluginError, instantiate_department
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _version(
    agents: list[dict[str, object]] | None = None, ptype: str = "department_template"
) -> m.CapaVersion:
    spec: dict[str, object] = {
        "frame": {"tools": {}, "kbs": [], "memory": {}},
        "agents": agents
        if agents is not None
        else [
            {
                "name": "Head of Sales",
                "role_title": "Lead",
                "mission": "sell",
                "is_team_lead": True,
                "persona": "# Lead",
                "skills": ["crm"],
            },
            {"name": "Rep A", "reports_to": "Head of Sales", "persona": "# A"},
            {"name": "Rep B", "reports_to": "Head of Sales", "persona": "# B"},
        ],
    }
    return m.CapaVersion(
        tenant_id=uuid.uuid4(),
        capa_id=uuid.uuid4(),
        semver="1.0.0",
        manifest={"name": "sales", "version": "1.0.0", "type": ptype, "department_template": spec},
        artifact_hash=b"x",
        permissions=[],
        capabilities=[],
        entry_points={},
    )


async def test_instantiate_creates_department_and_stopped_agents(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = _version()
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Sales")
        assert dept.name == "Sales" and dept.frame == {"tools": {}, "kbs": [], "memory": {}}
        agents = (
            (await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id)))
            .scalars()
            .all()
        )
        assert len(agents) == 3
        assert all(a.status == "stopped" for a in agents)
        lead = next(a for a in agents if a.is_team_lead)
        assert lead.name == "Head of Sales"
        assert dept.team_lead_agent_id == lead.id
        rep = next(a for a in agents if a.name == "Rep A")
        assert rep.definition["reports_to"] == "Head of Sales"
        assert rep.definition["persona"] == "# A"
        # each agent got an agent-tier memory store
        stores = (
            (await s.execute(select(m.MemoryStore).where(m.MemoryStore.tier == "agent")))
            .scalars()
            .all()
        )
        assert {st.owner_id for st in stores} == {a.id for a in agents}


async def test_wrong_type_rejected(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        with pytest.raises(PluginError):
            await instantiate_department(
                s, tenant_id=tenant, version=_version(ptype="agent_template"), name="X"
            )


async def test_duplicate_name_and_bad_reports_to_rejected(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        with pytest.raises(PluginError):
            await instantiate_department(
                s,
                tenant_id=tenant,
                name="X",
                version=_version(agents=[{"name": "A"}, {"name": "A"}]),
            )
        with pytest.raises(PluginError):
            await instantiate_department(
                s,
                tenant_id=tenant,
                name="X",
                version=_version(agents=[{"name": "A", "reports_to": "Ghost"}]),
            )


async def test_repeat_from_setup_hires_a_numbered_team_on_one_runtime(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        runtime = m.Capa(tenant_id=tenant, name="example_runtime", type="runtime_adapter")
        department_capa = m.Capa(
            tenant_id=tenant, name="example_department", type="department_template"
        )
        s.add(runtime)
        s.add(department_capa)
        await s.flush()
        s.add(m.CapaInstallation(tenant_id=tenant, capa_id=runtime.id, status="enabled"))
        s.add(
            m.CapaInstallation(
                tenant_id=tenant,
                capa_id=department_capa.id,
                status="enabled",
                config={"team_size": "3", "project": "Widgets"},
            )
        )
        await s.flush()
        version = _version(
            agents=[
                {
                    "name": "Senior {{n}}",
                    "is_team_lead": True,
                    "mission": "Work {{project}} as number {{n}}",
                    "repeat_from_setup": "team_size",
                    "runtime": "example_runtime",
                    "trigger": {
                        "kind": "cron",
                        "cron_expression": "*/5 * * * *",
                        "task_text": "Poll {{project}}",
                    },
                }
            ]
        )
        version.tenant_id = tenant
        version.capa_id = department_capa.id
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Desk")
        agents = (
            (await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id)))
            .scalars()
            .all()
        )
        by_name = {agent.name: agent for agent in agents}
        assert set(by_name) == {"Senior 1", "Senior 2", "Senior 3"}
        assert by_name["Senior 1"].is_team_lead
        assert dept.team_lead_agent_id == by_name["Senior 1"].id
        assert by_name["Senior 2"].definition["reports_to"] == "Senior 1"
        assert by_name["Senior 3"].mission == "Work Widgets as number 3"
        assert all(agent.runtime_ref == str(runtime.id) for agent in agents)
        # runtime_ref is versioned: v1 must already carry it, or every hired
        # agent opens with an unpublished change and runs without its runtime.
        for agent in agents:
            assert not (await draft_status(s, agent)).dirty
        triggers = (await s.execute(select(m.Trigger))).scalars().all()
        assert len(triggers) == 3
        assert {trigger.task_text for trigger in triggers} == {"Poll Widgets"}


async def test_blank_repeat_stays_one_agent_and_an_unnamed_runtime_stays_unset(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = _version(
            agents=[{"name": "Solo", "repeat_from_setup": "team_size", "mission": "Go"}]
        )
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Desk")
        agents = (
            (await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id)))
            .scalars()
            .all()
        )
        assert [agent.name for agent in agents] == ["Solo"]
        assert agents[0].runtime_ref is None


async def test_repeat_count_and_missing_runtime_are_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        capa = m.Capa(tenant_id=tenant, name="counted", type="department_template")
        s.add(capa)
        await s.flush()
        s.add(
            m.CapaInstallation(
                tenant_id=tenant, capa_id=capa.id, status="enabled", config={"team_size": "40"}
            )
        )
        await s.flush()
        version = _version(agents=[{"name": "A", "repeat_from_setup": "team_size"}])
        version.capa_id = capa.id
        with pytest.raises(PluginError, match="between 1 and 32"):
            await instantiate_department(s, tenant_id=tenant, version=version, name="X")
        installation = (
            await s.execute(select(m.CapaInstallation).where(m.CapaInstallation.capa_id == capa.id))
        ).scalar_one()
        installation.config = {"team_size": "several"}
        version.manifest = {
            **version.manifest,
            "department_template": {
                "frame": {},
                "agents": [{"name": "A", "repeat_from_setup": "team_size"}],
            },
        }
        with pytest.raises(PluginError, match="whole number"):
            await instantiate_department(s, tenant_id=tenant, version=version, name="X")
        version.manifest = {
            **version.manifest,
            "department_template": {
                "frame": {},
                "agents": [{"name": "A", "runtime": "missing_runtime"}],
            },
        }
        with pytest.raises(PluginError, match="not an installed runtime"):
            await instantiate_department(s, tenant_id=tenant, version=version, name="X")


async def test_instantiate_creates_trigger_when_agent_has_one(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = _version(
            agents=[
                {
                    "name": "Nora",
                    "is_team_lead": True,
                    "trigger": {
                        "kind": "cron",
                        "cron_expression": "0 8 * * 1-5",
                        "task_text": "Tagesreport erstellen",
                    },
                },
            ]
        )
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Sales")
        agent = (
            await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        trigger = (
            await s.execute(select(m.Trigger).where(m.Trigger.agent_id == agent.id))
        ).scalar_one()
        assert trigger.kind == "cron"
        assert trigger.cron_expression == "0 8 * * 1-5"
        assert trigger.task_text == "Tagesreport erstellen"
        assert trigger.enabled is True


async def test_instantiate_creates_skill_assignment_for_existing_local_skill(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        skill = m.Skill(tenant_id=tenant, name="crm", origin="local")
        s.add(skill)
        await s.flush()
        skill_version = m.SkillVersion(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={"schema_version": 1},
            artifact_hash=b"x",
        )
        s.add(skill_version)
        await s.flush()
        skill.current_version_id = skill_version.id
        await s.flush()

        version = _version(
            agents=[{"name": "Head of Sales", "is_team_lead": True, "skills": ["crm"]}]
        )
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Sales")
        agent = (
            await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        assignment = (
            await s.execute(select(m.SkillAssignment).where(m.SkillAssignment.agent_id == agent.id))
        ).scalar_one()
        assert assignment.skill_version_id == skill_version.id
        assert assignment.enabled is True


async def test_instantiate_handles_a_duplicate_skill_name_without_crashing(
    app_session: AppSessionFactory,
) -> None:
    """`Skill.name` has no unique constraint on `(tenant_id, name)` -- an
    archived skill sharing a name with a live one is a real (if unlikely)
    shape a hand-edited or re-exported capa could trigger. Before this fix,
    `_assign_named_skills` used `scalar_one_or_none()`, which raises
    `MultipleResultsFound` (not a `PluginError`) on two matching rows and
    would 500 the whole department instantiate rather than just skip or
    bind cleanly."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        archived = m.Skill(tenant_id=tenant, name="crm", origin="local")
        s.add(archived)
        await s.flush()
        archived.deleted_at = archived.created_at
        await s.flush()

        skill = m.Skill(tenant_id=tenant, name="crm", origin="local")
        s.add(skill)
        await s.flush()
        skill_version = m.SkillVersion(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={"schema_version": 1},
            artifact_hash=b"x",
        )
        s.add(skill_version)
        await s.flush()
        skill.current_version_id = skill_version.id
        await s.flush()

        version = _version(
            agents=[{"name": "Head of Sales", "is_team_lead": True, "skills": ["crm"]}]
        )
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Sales")
        agent = (
            await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        assignment = (
            await s.execute(select(m.SkillAssignment).where(m.SkillAssignment.agent_id == agent.id))
        ).scalar_one()
        assert assignment.skill_version_id == skill_version.id


async def test_instantiate_skips_missing_skill_silently(app_session: AppSessionFactory) -> None:
    """A named skill that doesn't (yet) exist in the target tenant -- e.g. its
    sibling `skill` capa in the ZIP hasn't been installed yet -- must not
    fail the whole department instantiate. Matches materialise.py's own
    "malformed data is logged and skipped, not raised" convention."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = _version(
            agents=[{"name": "Head of Sales", "is_team_lead": True, "skills": ["nonexistent"]}]
        )
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Sales")
        agent = (
            await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        assignments = (
            (
                await s.execute(
                    select(m.SkillAssignment).where(m.SkillAssignment.agent_id == agent.id)
                )
            )
            .scalars()
            .all()
        )
        assert assignments == []
