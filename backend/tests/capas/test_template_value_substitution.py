"""Unit tests for the generic {{field_key}} template-value substitution
mechanism (design doc: 2026-09-20-engineering-dev-agent-capa-design.md §1).
Integration coverage (instantiate_department wiring, real setup config,
the "no setup ever run" regression guard) lives in this same file, below
the pure-function tests -- see that section's own docstrings."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.capas.service import (
    _load_installation_config,
    _substitute_template_values,
    instantiate_department,
    install_plugin,
)
from tests.conftest import AppSessionFactory


def test_replaces_a_present_token() -> None:
    assert (
        _substitute_template_values("Check {{repo}}.", {"repo": "acme/widgets"})
        == "Check acme/widgets."
    )


def test_leaves_a_missing_token_literally_in_place() -> None:
    assert _substitute_template_values("Check {{repo}}.", {}) == "Check {{repo}}."


def test_text_with_no_tokens_is_unchanged() -> None:
    assert (
        _substitute_template_values("Plain mission text.", {"repo": "acme/widgets"})
        == "Plain mission text."
    )


def test_replaces_multiple_distinct_tokens() -> None:
    text = "Work {{repo}} issues labeled {{label}}."
    config = {"repo": "acme/widgets", "label": "agent-ready"}
    assert (
        _substitute_template_values(text, config)
        == "Work acme/widgets issues labeled agent-ready."
    )


def test_replaces_a_repeated_token_every_time_it_appears() -> None:
    text = "{{repo}} -- clone {{repo}} and work in it."
    config = {"repo": "acme/widgets"}
    assert (
        _substitute_template_values(text, config)
        == "acme/widgets -- clone acme/widgets and work in it."
    )


pytestmark = pytest.mark.asyncio


async def test_load_installation_config_returns_the_stored_plain_values(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data={"name": "cfg_probe", "version": "1.0.0", "type": "department_template"},
        )
        s.add(
            m.CapaInstallation(
                tenant_id=tenant,
                capa_id=version.capa_id,
                status="enabled",
                config={"repo": "acme/widgets"},
            )
        )
        await s.flush()

        config = await _load_installation_config(s, capa_id=version.capa_id)
        assert config == {"repo": "acme/widgets"}


async def test_load_installation_config_returns_empty_dict_when_no_row_exists(
    app_session: AppSessionFactory,
) -> None:
    """The common case: crm_vertrieb_agent, helpdesk_support_agent, and every
    test in test_instantiate_department.py/test_instantiate_agent.py install
    a version and instantiate straight away, with no CapaInstallation row
    ever created (that row is only created lazily by enable_plugin's
    get-or-create -- capas/lifecycle.py). Must return {}, not raise."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data={"name": "cfg_probe_2", "version": "1.0.0", "type": "department_template"},
        )
        config = await _load_installation_config(s, capa_id=version.capa_id)
        assert config == {}


async def test_instantiate_department_substitutes_setup_values_into_mission_and_trigger(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data={
                "name": "mini_dept",
                "version": "1.0.0",
                "type": "department_template",
                "setup": {
                    "title": "Set up",
                    "fields": [
                        {"key": "repo", "label": "Repo", "kind": "text"},
                        {"key": "label", "label": "Label", "kind": "text"},
                    ],
                },
                "department_template": {
                    "frame": {},
                    "agents": [
                        {
                            "name": "Lead",
                            "is_team_lead": True,
                            "mission": "Work {{repo}} issues labeled {{label}}.",
                            "persona": "# Lead for {{repo}}",
                            "role_title": "{{label}} handler",
                            "trigger": {
                                "kind": "cron",
                                "cron_expression": "*/20 * * * *",
                                "task_text": "Check {{repo}} for {{label}}.",
                            },
                        }
                    ],
                },
            },
        )
        s.add(
            m.CapaInstallation(
                tenant_id=tenant,
                capa_id=version.capa_id,
                status="enabled",
                config={"repo": "acme/widgets", "label": "agent-ready"},
            )
        )
        await s.flush()

        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Eng")
        agent = (
            await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        assert agent.mission == "Work acme/widgets issues labeled agent-ready."
        assert agent.definition["persona"] == "# Lead for acme/widgets"
        assert agent.role_title == "agent-ready handler"
        trigger = (
            await s.execute(select(m.Trigger).where(m.Trigger.agent_id == agent.id))
        ).scalar_one()
        assert trigger.task_text == "Check acme/widgets for agent-ready."


async def test_instantiate_department_leaves_tokens_literal_when_setup_was_never_run(
    app_session: AppSessionFactory,
) -> None:
    """Regression guard: every pre-existing template capa (crm_vertrieb_agent,
    helpdesk_support_agent, ...) has no [plugin.setup] block and is hired
    with no CapaInstallation row ever created for it. This must keep
    producing byte-identical agents, not blank/erroring ones."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        version = await install_plugin(
            s,
            tenant_id=tenant,
            manifest_data={
                "name": "mini_dept_no_setup",
                "version": "1.0.0",
                "type": "department_template",
                "department_template": {
                    "frame": {},
                    "agents": [
                        {
                            "name": "Lead",
                            "is_team_lead": True,
                            "mission": "Work {{repo}} issues.",
                        }
                    ],
                },
            },
        )
        dept = await instantiate_department(s, tenant_id=tenant, version=version, name="Eng")
        agent = (
            await s.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        assert agent.mission == "Work {{repo}} issues."
