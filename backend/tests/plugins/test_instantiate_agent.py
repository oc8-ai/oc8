from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import PluginError, install_plugin, instantiate_agent
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _agent_template_manifest(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": "Dana",
        "role_title": "Bookkeeper & Controller",
        "mission": "Keep the books accurate.",
        "persona": "# Dana\nPrecise and calm.",
        "skills": ["Month-end close"],
        "max_steps": 24,
    }
    spec.update(overrides)
    return {
        "name": "finance_bookkeeper",
        "version": "1.0.0",
        "type": "agent_template",
        "trust": "first_party",
        "summary": "Day-to-day accounting and month-end close.",
        "agent_template": spec,
        "skill_pack": {
            "skills": [
                {
                    "name": "Month-end close",
                    "category": "finance",
                    "description": "Run a controlled month-end close.",
                    "instruction": "Follow the close calendar and reconcile every balance sheet account.",
                    "guardrails": ["Never invent balances."],
                }
            ]
        },
    }


async def test_instantiate_agent_fills_mission_persona_and_memory(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        s.add(dept)
        await s.flush()
        version = await install_plugin(s, tenant_id=tenant, manifest_data=_agent_template_manifest())
        agent = await instantiate_agent(
            s,
            tenant_id=tenant,
            version=version,
            department_id=dept.id,
        )
        assert agent.name == "Dana"
        assert agent.role_title == "Bookkeeper & Controller"
        assert agent.mission == "Keep the books accurate."
        assert agent.status == "stopped"
        assert agent.definition["persona"] == "# Dana\nPrecise and calm."
        assert agent.definition["skills"] == ["Month-end close"]
        assert agent.definition["max_steps"] == 24
        assert agent.definition["plugin"] == "finance_bookkeeper"
        stores = (
            (
                await s.execute(
                    select(m.MemoryStore).where(
                        m.MemoryStore.tier == "agent", m.MemoryStore.owner_id == agent.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stores) == 1


async def test_instantiate_agent_respects_override_name(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        s.add(dept)
        await s.flush()
        version = await install_plugin(s, tenant_id=tenant, manifest_data=_agent_template_manifest())
        agent = await instantiate_agent(
            s,
            tenant_id=tenant,
            version=version,
            department_id=dept.id,
            name="Controller EU",
        )
        assert agent.name == "Controller EU"
        assert agent.mission == "Keep the books accurate."


async def test_instantiate_agent_rejects_wrong_type(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        s.add(dept)
        await s.flush()
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data={
                "name": "sales",
                "version": "1.0.0",
                "type": "department_template",
                "department_template": {"agents": [{"name": "A"}]},
            },
        )
        with pytest.raises(PluginError):
            await instantiate_agent(
                s, tenant_id=tenant, version=version, department_id=dept.id
            )


async def test_agent_template_skills_materialise_on_enable(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = await install_plugin(s, tenant_id=tenant, manifest_data=_agent_template_manifest())
        await enable_plugin(
            s,
            tenant_id=tenant,
            capa_id=version.capa_id,
            granted_permissions=[],
        )
        skills = (
            (await s.execute(select(m.Skill).where(m.Skill.tenant_id == tenant))).scalars().all()
        )
        assert [sk.name for sk in skills] == ["Month-end close"]


async def test_legacy_thin_agent_template_still_instantiates(
    app_session: AppSessionFactory,
) -> None:
    """Old manifests without [plugin.agent_template] keep working (name only)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        s.add(dept)
        await s.flush()
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data={"name": "legacy.mod", "version": "0.1.0"},
        )
        agent = await instantiate_agent(
            s, tenant_id=tenant, version=version, department_id=dept.id
        )
        assert agent.name == "legacy.mod"
        assert agent.mission == ""
        assert agent.definition["plugin"] == "legacy.mod"


async def test_instantiate_agent_creates_trigger_when_present(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        s.add(dept)
        await s.flush()
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data=_agent_template_manifest(
                trigger={
                    "kind": "cron",
                    "cron_expression": "0 9 * * *",
                    "task_text": "Daily reconciliation",
                }
            ),
        )
        agent = await instantiate_agent(
            s,
            tenant_id=tenant,
            version=version,
            department_id=dept.id,
        )
        trigger = (
            await s.execute(select(m.Trigger).where(m.Trigger.agent_id == agent.id))
        ).scalar_one()
        assert trigger.kind == "cron"
        assert trigger.cron_expression == "0 9 * * *"
        assert trigger.task_text == "Daily reconciliation"
        assert trigger.enabled is True
        # A trigger built by hand-constructing a Trigger row (instead of going
        # through triggers/service.py::create_trigger) never got next_run_at
        # set -- the scheduler only ever selects `next_run_at <= now`
        # (triggers/scheduler.py), so a NULL value can never be selected and
        # the trigger is permanently dead despite existing in the database.
        assert trigger.next_run_at is not None


async def test_instantiate_agent_rejects_an_invalid_cron_trigger(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        s.add(dept)
        await s.flush()
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data=_agent_template_manifest(
                trigger={
                    "kind": "cron",
                    "cron_expression": "not a cron expression",
                    "task_text": "Daily reconciliation",
                }
            ),
        )
        with pytest.raises(PluginError, match="invalid trigger"):
            await instantiate_agent(
                s,
                tenant_id=tenant,
                version=version,
                department_id=dept.id,
            )


async def test_instantiate_agent_substitutes_template_values_from_installation_config(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        s.add(dept)
        await s.flush()
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data=_agent_template_manifest(
                mission="Reconcile {{account}} every {{cadence}}.",
                trigger={
                    "kind": "cron",
                    "cron_expression": "0 9 * * *",
                    "task_text": "Reconcile {{account}}",
                },
            ),
        )
        s.add(
            m.CapaInstallation(
                tenant_id=tenant,
                capa_id=version.capa_id,
                status="enabled",
                config={"account": "cash-eur", "cadence": "daily"},
            )
        )
        await s.flush()

        agent = await instantiate_agent(
            s, tenant_id=tenant, version=version, department_id=dept.id
        )
        assert agent.mission == "Reconcile cash-eur every daily."
        trigger = (
            await s.execute(select(m.Trigger).where(m.Trigger.agent_id == agent.id))
        ).scalar_one()
        assert trigger.task_text == "Reconcile cash-eur"
