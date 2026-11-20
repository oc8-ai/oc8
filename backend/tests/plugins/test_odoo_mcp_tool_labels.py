"""odoo_mcp's real tool labels (run step timeline, Task 8).

Reads the REAL tool_pack.toml and the REAL i18n/de.po, the same way
test_odoo_mcp_manifest.py reads the real presets: these strings are what a
customer sees on a timeline, so editing the TOML without editing this test
must fail.
"""

from __future__ import annotations

from pathlib import Path

from oc8.capas.discovery import find_plugin
from oc8.capas.i18n import translations_for
from oc8.capas.manifest import ToolPackConnection, parse_manifest

# tests/plugins/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"

_TOOLS = {
    "search_records",
    "get_record",
    "list_models",
    "list_resource_templates",
    "aggregate_records",
    "create_record",
    "update_record",
    "delete_record",
    "post_message",
}


def _found() -> object:
    found = find_plugin("odoo_mcp", [str(_PLUGINS_DIR)])
    assert found is not None, "odoo_mcp plugin not found"
    assert found.valid, found.error
    return found


def _connection() -> ToolPackConnection:
    found = _found()
    assert found.manifest is not None  # type: ignore[attr-defined]
    manifest = parse_manifest(found.manifest)  # type: ignore[attr-defined]
    assert manifest.tool_pack is not None
    return manifest.tool_pack.connections[0]


def test_every_tool_the_pack_offers_has_a_label() -> None:
    """A tool without one falls back to "Read from odoo", which is correct but
    is not what this pack promised. All nine, or the promise is partial."""
    assert set(_connection().tool_labels) == _TOOLS


def test_each_label_has_an_english_verb_and_a_running_form() -> None:
    for name, label in _connection().tool_labels.items():
        assert label.verb, name
        assert label.running, name


def test_the_model_labels_cover_the_models_these_agents_work_on() -> None:
    labels = _connection().model_labels
    assert labels["crm.lead"] == "deals"
    assert labels["helpdesk.ticket"] == "tickets"
    assert set(labels) >= {"crm.lead", "sale.order", "res.partner", "helpdesk.ticket"}


def test_create_record_does_not_reference_an_id_it_cannot_have() -> None:
    """A create has no id yet, so an {id} there would make the label fall back
    on every single create."""
    assert "{id}" not in _connection().tool_labels["create_record"].object


def test_every_label_string_is_translated_into_german() -> None:
    """The i18n catalog, not a verb_de field, is where German lives -- so the
    catalog is what has to be complete."""
    found = _found()
    i18n = found.i18n  # type: ignore[attr-defined]
    conn = _connection()
    missing: list[str] = []
    for name, label in conn.tool_labels.items():
        for field, source in (("verb", label.verb), ("object", label.object),
                              ("running", label.running)):
            if source and "de" not in translations_for(i18n, source):
                missing.append(f"{name}.{field}: {source!r}")
    for key, source in conn.model_labels.items():
        if "de" not in translations_for(i18n, source):
            missing.append(f"model_labels[{key}]: {source!r}")
    assert not missing, "untranslated: " + "; ".join(missing)


def test_no_label_string_is_german() -> None:
    """The hard rule: authored manifest strings are English. A German label
    here would be invisible to every other locale and would break the
    catalog lookup above (the msgid IS the English source)."""
    german_markers = ("ä", "ö", "ü", "ß", "Sucht ", "Legt ", "Bearbeitet")
    conn = _connection()
    for name, label in conn.tool_labels.items():
        for source in (label.verb, label.object, label.running):
            assert not any(marker in source for marker in german_markers), f"{name}: {source!r}"
