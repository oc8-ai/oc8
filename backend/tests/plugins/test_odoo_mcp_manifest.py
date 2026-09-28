"""The odoo_mcp plugin ships three guardrail presets (task 3 of the plugin
guardrail presets design doc): read_only, assist_with_approval (recommended)
and autonomous_with_limit. This reads the REAL plugin.toml -- editing the
TOML without editing this test must fail, since these values are a security
posture, not configuration."""

from __future__ import annotations

from pathlib import Path

from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import ToolPackConnection, parse_manifest

# tests/plugins/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


def _connection() -> ToolPackConnection:
    found = find_plugin("odoo_mcp", [str(_PLUGINS_DIR)])
    assert found is not None, "odoo_mcp plugin not found"
    assert found.valid, found.error
    assert found.manifest is not None
    manifest = parse_manifest(found.manifest)
    assert manifest.tool_pack is not None
    return manifest.tool_pack.connections[0]


class TestOdooMcpGuardrailPresets:
    def test_ships_exactly_three_presets(self) -> None:
        conn = _connection()
        assert {p.key for p in conn.guardrail_presets} == {
            "read_only",
            "assist_with_approval",
            "autonomous_with_limit",
            "internal_only",
            "no_deletions",
        }

    def test_exactly_one_is_recommended(self) -> None:
        conn = _connection()
        recommended = [p.key for p in conn.guardrail_presets if p.recommended]
        assert recommended == ["assist_with_approval"]

    def test_read_only_grants_read_alone(self) -> None:
        p = next(p for p in _connection().guardrail_presets if p.key == "read_only")
        assert (p.read, p.modify, p.approval_actions, p.approval_eur) == (
            True,
            False,
            [],
            None,
        )

    def test_assist_with_approval_requires_approval_on_every_send(self) -> None:
        p = next(p for p in _connection().guardrail_presets if p.key == "assist_with_approval")
        assert (p.read, p.modify) == (True, True)
        assert p.approval_actions == ["modify"]
        assert p.approval_eur is None

    def test_autonomous_with_limit_gates_only_above_1000_euro(self) -> None:
        p = next(p for p in _connection().guardrail_presets if p.key == "autonomous_with_limit")
        assert (p.read, p.modify) == (True, True)
        assert p.approval_actions == []
        assert p.approval_eur == 1000


def test_the_autonomous_preset_cannot_delete() -> None:
    """A limit with a hole where it cannot reach is not a limit.

    Odoo classifies `delete_record` as a `send`, and a deletion carries no
    monetary value. `authz.pdp.authorize_tool` counts an unreadable value as
    zero, and zero never meets a EUR 1000 threshold -- so "autonomous, approval
    above 1000" would have let an agent delete records unattended, under a name
    promising the opposite. The tool is out of reach in this preset instead.
    """
    presets = {p.key: p for p in _connection().guardrail_presets}
    autonomous = presets["autonomous_with_limit"]

    assert autonomous.only, "an empty `only` means every tool, including delete_record"
    assert "delete_record" not in autonomous.only
    # The rest of the connection's tools stay available, or the preset would be
    # a differently-named read_only.
    assert {"create_record", "update_record", "post_message"} <= set(autonomous.only)


def test_the_approval_preset_needs_no_allowlist_because_a_human_sees_every_send() -> None:
    """The contrast that shows why only `autonomous_with_limit` needs one."""
    presets = {p.key: p for p in _connection().guardrail_presets}
    assist = presets["assist_with_approval"]

    assert assist.approval_actions == ["modify"]
    assert not assist.only, "every send already reaches a person; no tool needs hiding"


def test_no_preset_grants_write_because_no_odoo_tool_needs_it() -> None:
    """A right that confers nothing must not be advertised as granted.

    This connection's `scopes` classify every tool as `read` or `send`; not one
    is `write` -- and after collapsing write/send into the single `modify`
    field, `read_only` is the one preset that needs neither, so it alone should
    have `modify is False`. Every other preset legitimately needs `modify`
    (collapsed from `send`) for the tools it grants.
    """
    for preset in _connection().guardrail_presets:
        if preset.key == "read_only":
            assert preset.modify is False, f"{preset.key} should not need modify"
        else:
            assert preset.modify is True, f"{preset.key} should need modify (collapsed from send)"


def test_internal_only_cannot_reach_the_customer() -> None:
    """`post_message` is this connection's only outward tool.

    Rights cannot express "may work the CRM but never message a customer",
    because post_message shares the `send` right with create/update. Withholding
    the tool is the whole preset, so this is the assertion that it means what
    its name says.
    """
    p = next(p for p in _connection().guardrail_presets if p.key == "internal_only")
    assert p.only, "an empty allowlist would put post_message back within reach"
    assert "post_message" not in p.only
    assert "delete_record" not in p.only
    assert {"create_record", "update_record"} <= set(p.only)


def test_connection_config_declares_capability_seam_keys() -> None:
    """Neutral seam keys under `[connections.config]` drive destructive,
    irreversible, lookup and approval gating without Odoo-specific core code."""
    cfg = _connection().config
    assert cfg.get("destructive_tools") == ["delete_record"]
    assert cfg.get("irreversible_tools") == []
    assert cfg.get("read_before_write") is True
    assert cfg.get("entity_lookup_tools") == ["search_records"]
    assert cfg.get("approval_templates") == {
        "post_message": "Allow {connection} to post a message on {record}?",
        "delete_record": "Allow {connection} to delete {record}?",
    }
    assert "preview" not in cfg


def test_no_deletions_differs_from_assist_by_never_rather_than_ask() -> None:
    """The two are only worth shipping separately if they actually differ.

    `assist_with_approval` holds a deletion for a human, who can wave it through.
    `no_deletions` takes the tool off the table. Same approval rule, different
    reach -- if that stops being true, one of them is redundant.
    """
    presets = {p.key: p for p in _connection().guardrail_presets}
    assist, never = presets["assist_with_approval"], presets["no_deletions"]

    assert assist.approval_actions == never.approval_actions == ["modify"]
    assert not assist.only, "assist withholds nothing; a human sees every send"
    assert "delete_record" not in never.only
    assert "post_message" in never.only, "only deletion is withheld, not customer contact"
