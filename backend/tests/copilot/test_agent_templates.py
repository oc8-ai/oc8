from __future__ import annotations

from oc8.copilot.agent_templates import (
    CORE_TEMPLATES,
    MAX_IDEAS,
    AgentTemplate,
    merge_templates,
    pick_ideas,
)


def test_the_core_catalogue_is_complete_and_starts_agent_mode() -> None:
    assert len(CORE_TEMPLATES) == 8
    assert len({t.key for t in CORE_TEMPLATES}) == 8
    for t in CORE_TEMPLATES:
        assert t.prompt_en.startswith("/agent ") and t.prompt_de.startswith("/agent ")
        assert "!" not in t.title_de + t.subtitle_de + t.prompt_de


def test_connected_systems_come_first_then_generic_ones() -> None:
    ideas = pick_ideas(CORE_TEMPLATES, frozenset({"odoo_mcp"}))
    assert len(ideas) <= MAX_IDEAS
    assert ideas[0].key == "invoice_checker"
    assert all("odoo_mcp" in t.requires_any or not t.requires_any for t in ideas)


def test_without_connections_only_generic_ideas_show() -> None:
    ideas = pick_ideas(CORE_TEMPLATES, frozenset())
    assert ideas and all(not t.requires_any for t in ideas)


def test_an_edition_adds_templates_but_cannot_replace_core_ones() -> None:
    extra = AgentTemplate(
        key="invoice_checker",
        title_en="X",
        title_de="X",
        subtitle_en="",
        subtitle_de="",
        requires_any=(),
        prompt_en="/agent x",
        prompt_de="/agent x",
    )
    new = AgentTemplate(
        key="dunning",
        title_en="Dunning",
        title_de="Mahnwesen",
        subtitle_en="",
        subtitle_de="",
        requires_any=("odoo_mcp",),
        prompt_en="/agent dunning",
        prompt_de="/agent Mahnwesen",
    )
    merged = merge_templates(CORE_TEMPLATES, [extra, new])
    assert [t.key for t in merged].count("invoice_checker") == 1
    assert merged[-1].key == "dunning"
