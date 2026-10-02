"""`tool_labels` / `model_labels` on a tool-pack connection
(run step timeline, Task 7).

Parsed from a synthetic manifest table, not from a real plugin folder --
odoo_mcp's own labels are asserted against the REAL file in
tests/plugins/test_odoo_mcp_tool_labels.py (Task 8).
"""

from __future__ import annotations

from typing import Any

import pytest

from oc8.capas.manifest import ManifestError, ToolPackConnection, parse_manifest


def _table(**connection: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": "primary",
        "name": "odoo",
        "server_url": "",
        "transport": "stdio",
        "scopes": {"read": ["search_records"], "modify": ["create_record"]},
    }
    base.update(connection)
    return {
        "name": "labelled_pack",
        "version": "1.0.0",
        "type": "tool_pack",
        "trust": "first_party",
        "summary": "a pack with labels",
        "tool_pack": {"connections": [base]},
    }


def _connection(**connection: Any) -> ToolPackConnection:
    manifest = parse_manifest(_table(**connection))
    assert manifest.tool_pack is not None
    return manifest.tool_pack.connections[0]


def test_a_connection_with_no_labels_parses_and_carries_empty_maps() -> None:
    conn = _connection()
    assert conn.tool_labels == {}
    assert conn.model_labels == {}


def test_a_label_parses_its_three_english_strings() -> None:
    conn = _connection(
        tool_labels={
            "search_records": {
                "verb": "Looked up",
                "object": "{model_label}",
                "running": "Looking up {model_label}",
            }
        },
        model_labels={"crm.lead": "deals"},
    )
    label = conn.tool_labels["search_records"]
    assert label.verb == "Looked up"
    assert label.object == "{model_label}"
    assert label.running == "Looking up {model_label}"
    assert conn.model_labels["crm.lead"] == "deals"


def test_object_and_running_are_optional() -> None:
    conn = _connection(tool_labels={"search_records": {"verb": "Looked things up"}})
    assert conn.tool_labels["search_records"].object == ""
    assert conn.tool_labels["search_records"].running == ""


def test_a_label_for_an_unknown_tool_is_refused_by_name() -> None:
    """The spec's own example named `search_read`, which this pack does not
    have. A silently-ignored label is indistinguishable from no label at all."""
    with pytest.raises(ManifestError) as exc:
        _connection(tool_labels={"search_read": {"verb": "Looked up"}})
    assert "search_read" in str(exc.value)


def test_an_unknown_field_on_a_label_is_refused() -> None:
    """extra="forbid", same as every other manifest model: `verb_de` (the
    spec's inline-German sketch) must fail loudly rather than be dropped, so
    whoever writes it is sent to the plugin's i18n/de.po instead."""
    with pytest.raises(ManifestError) as exc:
        _connection(tool_labels={"search_records": {"verb": "Looked up", "verb_de": "Nachgesehen"}})
    assert "verb_de" in str(exc.value)


def test_a_label_on_a_connection_with_flat_scopes_is_not_validated() -> None:
    """Nothing to check a key against when the pack never classified its
    tools -- refusing there would reject a legitimate manifest."""
    conn = _connection(
        scopes=["search_records", "create_record"],
        tool_labels={"anything_at_all": {"verb": "Did something"}},
    )
    assert "anything_at_all" in conn.tool_labels
