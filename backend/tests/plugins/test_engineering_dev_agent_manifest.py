"""engineering_dev_agent's shape off disk: a department_template that pulls
in github_mcp via plugin_depends (NOT depends -- see this plan's Global
Constraints for why), ships one team-lead agent with {{repo}}/{{label}}
tokens in its mission/trigger, asks for repo/label/base_branch on its own
setup form, and grants the github connection exactly what github_mcp's own
dev_assistant_merge_needs_approval guardrail preset grants."""

from __future__ import annotations

from pathlib import Path

from oc8.authz.pdp import Effect, authorize_tool_call, effective_tool_policies
from oc8.capas.discovery import DiscoveredPlugin, find_plugin
from oc8.capas.guardrails import Guardrail
from oc8.capas.manifest import parse_manifest

# tests/plugins/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


def _discovered() -> DiscoveredPlugin:
    found = find_plugin("engineering_dev_agent", [str(_PLUGINS_DIR)])
    assert found is not None, "engineering_dev_agent plugin not found"
    assert found.valid, found.error
    return found


def _github_mcp_preset(key: str) -> Guardrail:
    found = find_plugin("github_mcp", [str(_PLUGINS_DIR)])
    assert found is not None and found.guardrail_library is not None
    return next(g for g in found.guardrail_library.guardrail if g.key == key)


def test_plugin_is_first_party_department_template_depending_on_github_mcp() -> None:
    found = _discovered()
    assert found.type == "department_template"
    assert found.trust == "first_party"
    assert found.manifest is not None
    assert found.manifest.get("plugin_depends") == ["github_mcp"]


def test_ships_exactly_one_team_lead_agent_with_a_twenty_minute_cron_trigger() -> None:
    found = _discovered()
    assert found.manifest is not None
    manifest = parse_manifest(found.manifest)
    assert manifest.department_template is not None
    agents = manifest.department_template.agents
    assert len(agents) == 1
    agent = agents[0]
    assert agent.is_team_lead is True
    assert agent.trigger is not None
    assert agent.trigger.kind == "cron"
    assert agent.trigger.cron_expression == "*/20 * * * *"


def test_mission_persona_role_title_and_trigger_carry_the_setup_tokens() -> None:
    found = _discovered()
    assert found.manifest is not None
    agent = parse_manifest(found.manifest).department_template.agents[0]
    assert "{{repo}}" in agent.mission
    assert "{{label}}" in agent.mission
    assert "{{repo}}" in agent.trigger.task_text
    assert "{{label}}" in agent.trigger.task_text


def test_setup_form_asks_for_repo_label_and_base_branch() -> None:
    found = _discovered()
    assert found.manifest is not None
    setup = parse_manifest(found.manifest).setup
    assert setup is not None
    fields_by_key = {f.key: f for f in setup.fields}
    assert set(fields_by_key) == {"repo", "label", "base_branch"}
    assert fields_by_key["repo"].required is True
    assert fields_by_key["label"].required is True
    assert fields_by_key["label"].default == "agent-ready"
    assert fields_by_key["base_branch"].required is False
    # No [plugin.setup.mcp]: this capa has no connection of its own to
    # create -- it only stores plain values, read back via
    # _load_installation_config (capas/service.py).
    assert setup.mcp is None


def test_frame_grants_github_the_same_rights_as_the_dev_assistant_preset() -> None:
    found = _discovered()
    assert found.manifest is not None
    frame = parse_manifest(found.manifest).department_template.frame
    grant = frame["tools"]["github"]
    preset = _github_mcp_preset("dev_assistant_merge_needs_approval")

    assert grant["read"] == preset.read
    assert grant["modify"] == preset.modify
    assert grant.get("approval_eur") == preset.approval_eur
    assert set(grant.get("approval_actions", [])) == set(preset.approval_actions)
    assert "only" not in grant  # unrestricted, same as the preset's only == ()


def test_frame_authorizes_pr_creation_and_gates_merge_via_the_real_pdp() -> None:
    """The regression test for the write/send vs. modify bug: goes through
    the actual PDP, not just the manifest dict, so a future re-introduction
    of an unrecognised key here fails a test instead of silently degrading
    the hired agent to read-only."""
    found = _discovered()
    assert found.manifest is not None
    frame = parse_manifest(found.manifest).department_template.frame
    policies = effective_tool_policies(frame, {})

    push = authorize_tool_call(
        policies=policies,
        connection_key="github",
        right="modify",
        tool="create_pull_request",
        value=None,
    )
    assert push.effect is Effect.ALLOW

    merge = authorize_tool_call(
        policies=policies,
        connection_key="github",
        right="modify",
        tool="merge_pull_request",
        value=None,
    )
    assert merge.effect is Effect.REQUIRE_APPROVAL
