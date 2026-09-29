"""odoo_mcp's `record_url` declaration, read from the REAL tool_pack.toml
(§6). Editing that file without editing this test must fail: every entity
listed here is a link an approver will click, and every entity NOT listed is
an approval that silently has no link.

Mirrors test_odoo_mcp_manifest.py's own harness exactly.
"""

from __future__ import annotations

from pathlib import Path

from oc8.approvals.record_url import resolve_record_url
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


def test_it_declares_a_record_url() -> None:
    assert _connection().record_url is not None


def test_the_base_url_comes_from_the_connections_own_env() -> None:
    """Not a literal: the same pack points at every customer's own host, and
    `credential_env_fields` rewrites this key from the attached login."""
    spec = _connection().record_url
    assert spec is not None
    assert spec.base_url_path == ["env", "ODOO_URL"]


def test_every_entity_focus_spec_labels_is_linkable() -> None:
    """The two tables must not drift: an entity the live log names is one an
    approval can be raised about, and a link that exists for four of six
    models is a link an approver stops trusting."""
    conn = _connection()
    spec = conn.record_url
    assert spec is not None
    labelled = set((conn.config.get("focus_spec") or {}).get("labels", {}))
    assert labelled - set(spec.models) == set(), "a labelled entity has no record URL"


def test_it_resolves_against_this_packs_own_config() -> None:
    conn = _connection()
    assert (
        resolve_record_url(conn.record_url, config=conn.config, entity="sale.order", ref="42")
        == "http://host.docker.internal:1000/odoo/sale.order/42"
    )


def test_an_entity_it_does_not_declare_gets_no_link() -> None:
    conn = _connection()
    assert (
        resolve_record_url(conn.record_url, config=conn.config, entity="res.users", ref="1") is None
    )
