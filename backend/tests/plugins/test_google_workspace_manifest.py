"""Capability seam keys on the google_workspace tool pack connection config."""

from __future__ import annotations

from pathlib import Path

from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import ToolPackConnection, parse_manifest

_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


def _connection() -> ToolPackConnection:
    found = find_plugin("google_workspace", [str(_PLUGINS_DIR)])
    assert found is not None, "google_workspace plugin not found"
    assert found.valid, found.error
    assert found.manifest is not None
    manifest = parse_manifest(found.manifest)
    assert manifest.tool_pack is not None
    return manifest.tool_pack.connections[0]


def test_connection_config_declares_capability_seam_keys() -> None:
    cfg = _connection().config
    assert cfg.get("destructive_tools") == ["drive_delete", "calendar_cancel_event"]
    assert cfg.get("irreversible_tools") == ["gmail_send", "gmail_reply"]
    assert cfg.get("read_before_write") is True
    assert cfg.get("entity_lookup_tools") == ["gmail_search", "calendar_list_events"]
    assert "preview" not in cfg
