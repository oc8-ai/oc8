"""Prompt starters an agent template ships (§5.3).

A round trip, not just a field: a capa exported from a tenant and installed
into another must bring its starters, or the feature silently only works for
agents somebody typed by hand.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from oc8 import models as m
from oc8.capas.discovery import discover_plugins
from oc8.capas.manifest import TemplateAgent, parse_manifest
from oc8.capas.service import install_plugin, instantiate_agent
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/plugins/<this> -> tests -> backend -> repo root, where capas/ lives.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGINS_DIR = _REPO_ROOT / "capas"


def test_a_template_may_declare_starters() -> None:
    agent = TemplateAgent(name="Nora", prompt_starters=["Wie viele Tickets sind offen?"])
    assert agent.prompt_starters == ["Wie viele Tickets sind offen?"]


def test_a_template_without_any_is_an_empty_list() -> None:
    assert TemplateAgent(name="Nora").prompt_starters == []


def test_too_many_starters_are_refused() -> None:
    """Six is already more than a composer can show; a picker is not a manual."""
    with pytest.raises(ValueError):
        TemplateAgent(name="Nora", prompt_starters=[f"q{i}" for i in range(7)])


def test_an_over_long_starter_is_refused() -> None:
    with pytest.raises(ValueError):
        TemplateAgent(name="Nora", prompt_starters=["x" * 200])


async def test_instantiating_writes_them_onto_the_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Support", frame={})
        db.add(dept)
        await db.flush()
        version = await install_plugin(
            db,
            tenant_id=tenant,
            manifest_data={
                "name": "helpdesk_agent",
                "version": "1.0.0",
                "type": "agent_template",
                "summary": "s",
                "agent_template": {
                    "name": "Sina",
                    "prompt_starters": ["Welche Tickets sind heute neu?"],
                },
            },
        )
        agent = await instantiate_agent(
            db, tenant_id=tenant, version=version, department_id=dept.id
        )
        assert (agent.presentation or {})["prompt_starters"] == ["Welche Tickets sind heute neu?"]


def test_the_helpdesk_support_agent_pack_ships_three_starters() -> None:
    """The first real consumer of this field (Step 9) -- covered by a test
    rather than by hand."""
    found = {p.plugin_id: p for p in discover_plugins([str(_PLUGINS_DIR)])}
    p = found["helpdesk_support_agent"]
    assert p.manifest is not None
    manifest = parse_manifest(p.manifest)
    assert manifest.department_template is not None
    sina = next(a for a in manifest.department_template.agents if a.name == "Sina")
    assert sina.prompt_starters == [
        "Which tickets came in today?",
        "Summarise the oldest open ticket",
        "What is waiting on a customer reply?",
    ]
