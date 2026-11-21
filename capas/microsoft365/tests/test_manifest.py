"""The manifest half of this plugin, now that it is SPLIT across
`plugin.toml` + `tool_pack.toml` + `guardrails/*.toml` + `setup/*.toml`
(design §2/§3).

These tests deliberately go through `oc8.capas.discovery.find_plugin` rather
than parsing `plugin.toml` alone: after the split, `plugin.toml` on its own is
only the slim core, and it is discovery's assembly of the sibling files that
has to be right. A test that read just `plugin.toml` would now pass while the
tool pack, the presets and the whole setup form were silently missing.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import Manifest, parse_manifest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_ROOT.parent


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge`/`events` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY on fixture setup AND teardown:
    before, so a sibling plugin's cached copy cannot answer our import; after,
    so nothing generic is left cached for anyone else. The teardown half is
    the load-bearing one -- `loader.import_entry_point` (Task 6's collision
    fix) only evicts modules IT ITSELF introduced, so a name left cached here
    makes a later `find_plugin`/`load_plugin` for gdrive_source or
    google_workspace silently hand back THIS plugin's code. Verified live.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge", "events"}
        or n.startswith(("connector.", "mcp_bridge.", "events."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's packages the ones that resolve, for each test.

    Function-scoped and autouse, per Task 7's convention: this file's plugin
    imports sit INSIDE test bodies and resolve at execution time, long after
    any collection-time module-top prelude would have run.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


def _load_toml(relative: str) -> dict[str, Any]:
    return tomllib.loads((PLUGIN_ROOT / relative).read_text())


def _manifest() -> Manifest:
    """The ASSEMBLED manifest, exactly as the core builds it from the split
    files -- re-parsed into the typed model so these tests can reach through
    `.tool_pack` / `.setup` the way every real consumer does."""
    discovered = find_plugin("microsoft365", [str(PLUGINS_ROOT)])
    assert discovered is not None, "microsoft365 was not discovered at all"
    assert discovered.valid, f"microsoft365 manifest is invalid: {discovered.error}"
    assert discovered.manifest is not None
    return parse_manifest(discovered.manifest)


def test_plugin_toml_parses_as_a_valid_manifest() -> None:
    manifest = _manifest()
    assert manifest.type == "tool_pack"
    assert manifest.entry_points["connectors"] == "connector.connector:register"
    assert manifest.tool_pack is not None
    assert len(manifest.tool_pack.connections) == 1


def test_the_slim_core_no_longer_carries_the_split_out_tables() -> None:
    """Hard cutover (design, Global Constraints): a leftover `[plugin.tool_pack]`
    or `[plugin.setup]` inline table, or a flat `guardrails.toml`, makes
    discovery reject the whole plugin rather than silently strip it -- so
    asserting they are gone here is asserting the split actually happened,
    not merely that the new files exist."""
    core = _load_toml("plugin.toml")["plugin"]
    assert "tool_pack" not in core
    assert "setup" not in core
    assert not (PLUGIN_ROOT / "guardrails.toml").is_file()
    assert (PLUGIN_ROOT / "tool_pack.toml").is_file()
    assert (PLUGIN_ROOT / "setup" / "fields.toml").is_file()


def test_scopes_cover_every_tool_and_nothing_else() -> None:
    from mcp_bridge.__main__ import ALL_TOOLS

    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    # `scopes` is `list[str] | dict[str, Any]` in the model -- the flat-list
    # form is the legacy one; this pack uses the read/send mapping.
    assert isinstance(conn.scopes, dict)
    declared = set(conn.scopes.get("read", [])) | set(conn.scopes.get("modify", []))
    actual = {t.name for t in ALL_TOOLS}
    assert declared == actual, f"manifest/tool mismatch: {declared ^ actual}"


def test_outward_tools_lives_in_config_not_scopes() -> None:
    # Regression: outward_tools must sit in the connection's `config` table,
    # not `scopes` -- that is the ONLY place any real consumer reads it from
    # (agent/engine.py, agent/outward.py, api/mcp_gateway.py,
    # api/v1/internal_agent.py all do `cfg.get("outward_tools")` off
    # `connection.config`). A copy stuck in `scopes` instead is a silent
    # no-op: the "at most one outward message per record per task" rule never
    # fires, with no error and no test failure -- exactly the bug this test
    # exists to catch mechanically.
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    # Mail only: Graph has no app-only permission for sending a Teams message,
    # so this pack ships no Teams send tool at all (design doc §4.1b).
    assert conn.config.get("outward_tools") == ["mail_send", "mail_reply"]
    assert isinstance(conn.scopes, dict)
    assert "outward_tools" not in conn.scopes


def test_the_bridge_launch_command_matches_the_package_on_disk() -> None:
    """`tool_pack.toml` and `setup/mcp.toml` both launch the SAME bridge, and
    the module they name has to be the package that actually exists -- a stale
    `-m microsoft365_mcp` here fails only at container runtime, where nothing
    in this suite would ever see it."""
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    assert conn.config["args"] == ["-m", "mcp_bridge"]
    assert conn.config["env"]["PYTHONPATH"] == "/app/capas/microsoft365"
    assert manifest.setup is not None
    assert manifest.setup.mcp is not None
    assert manifest.setup.mcp.args == conn.config["args"]
    assert (PLUGIN_ROOT / "mcp_bridge" / "__main__.py").is_file()


def test_the_setup_form_survived_the_split_intact() -> None:
    manifest = _manifest()
    assert manifest.setup is not None
    assert manifest.setup.title == "Connect Microsoft 365"
    assert [f.key for f in manifest.setup.fields] == [
        "app",
        "department",
        "default_user",
        "site_ids",
        "drive_ids",
    ]
    assert manifest.setup.oauth_provision is not None
    assert manifest.setup.oauth_provision.provider == "microsoft"
    assert manifest.setup.oauth_provision.connector_type == "microsoft365_files"
    assert manifest.setup.oauth_provision.drive_ids_field == "drive_ids"


def test_the_five_presets_and_five_library_entries_survived_the_split() -> None:
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    assert {p.key for p in conn.guardrail_presets} == {
        "read_only",
        "assist_with_approval",
        "autonomous_with_limit",
        "internal_only",
        "no_deletions",
    }
    # Exactly one preset is the recommended default; more than one would make
    # the setup screen's own choice arbitrary.
    assert [p.key for p in conn.guardrail_presets if p.recommended] == ["assist_with_approval"]

    discovered = find_plugin("microsoft365", [str(PLUGINS_ROOT)])
    assert discovered is not None and discovered.guardrail_library is not None
    assert {g.key for g in discovered.guardrail_library.guardrail} == {
        "mail_assistant_drafts_only",
        "mail_and_calendar_only",
        "sharepoint_knowledge_worker_read_only",
        "full_assistant_with_send_approval",
        "docs_editor_no_mail_no_teams",
    }


def test_guardrail_only_lists_reference_real_tool_names() -> None:
    """Covers BOTH kinds now that presets and the curated library live side by
    side in `guardrails/` -- before the split only the library file was
    checked, and a preset's `only` list could name a tool that does not
    exist."""
    from mcp_bridge.__main__ import ALL_TOOLS

    actual = {t.name for t in ALL_TOOLS}
    checked = 0
    for path in sorted((PLUGIN_ROOT / "guardrails").glob("*.toml")):
        entry = tomllib.loads(path.read_text())
        assert entry["kind"] in ("preset", "library"), path
        assert entry["key"] == path.stem, f"{path}: key does not match filename"
        for name in entry.get("only", []):
            assert name in actual, f"guardrails/{path.name} names unknown tool {name!r}"
        checked += 1
    assert checked == 10, f"expected 5 presets + 5 library entries, found {checked}"


def test_the_capa_is_at_version_one_two() -> None:
    assert _manifest().version == "1.2.0"


def test_it_declares_mail_received_as_a_polled_event_on_the_default_mailbox() -> None:
    from oc8.capas.manifest import TriggerConfigField, TriggerDecl

    manifest = _manifest()
    assert manifest.entry_points["event_sources"] == "events.source:register"
    [decl] = manifest.triggers
    assert isinstance(decl, TriggerDecl)
    assert (decl.type, decl.label, decl.polled, decl.editor_label) == (
        "mail.received",
        "New email",
        True,
        "On new email",
    )
    assert decl.description
    assert decl.config == [
        TriggerConfigField(key="mailbox", label="Mailbox", default_from="default_user"),
        TriggerConfigField(key="folder", label="Folder", default="inbox"),
    ]
    # The default mailbox is read from the setup's plain `default_user`
    # field, which the connection keeps in its env (and not as a secret).
    assert manifest.setup is not None and manifest.setup.mcp is not None
    assert manifest.setup.mcp.env_fields["MICROSOFT365_DEFAULT_USER"] == "default_user"
    assert "default_user" not in manifest.setup.mcp.secret_env_fields.values()
    kinds = {f.key: f.kind for f in manifest.setup.fields}
    assert kinds["default_user"] == "text"


def test_loading_the_capa_registers_the_poller_for_the_oauth_login() -> None:
    from oc8.capas.contributions import pollers_for
    from oc8.capas.loader import load_plugin

    discovered = find_plugin("microsoft365", [str(PLUGINS_ROOT)])
    assert discovered is not None
    assert load_plugin(discovered)
    registered = pollers_for("microsoft365")["mail.received"]
    # No credential type: the trigger listens on the Microsoft connection.
    assert registered.credential_type is None
    assert callable(registered.poller)


def test_the_trigger_strings_have_a_german_translation() -> None:
    import polib

    catalog = {e.msgid: e.msgstr for e in polib.pofile(str(PLUGIN_ROOT / "i18n" / "de.po"))}
    shown: list[str] = []
    for trigger in _load_toml("plugin.toml")["plugin"]["triggers"]:
        shown += [trigger["label"], trigger["description"], trigger["editor_label"]]
        shown += [setting["label"] for setting in trigger.get("config", [])]
    assert [s for s in shown if not catalog.get(s)] == []
    for msgstr in catalog.values():
        assert " Sie " not in f" {msgstr} " and "Ihr" not in msgstr, msgstr
