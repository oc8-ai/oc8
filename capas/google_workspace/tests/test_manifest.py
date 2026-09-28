"""The manifest half of this plugin, now that it is SPLIT across
`plugin.toml` + `tool_pack.toml` + `guardrails/*.toml` + `setup/*.toml`
(design §2/§3).

These tests deliberately go through `oc8.capas.discovery.find_plugin` rather
than parsing `plugin.toml` alone: after the split, `plugin.toml` on its own is
only the slim core, and it is discovery's assembly of the sibling files that
has to be right. A test that read just `plugin.toml` would now pass (or blow up
for the wrong reason) while the tool pack, the presets and the whole setup form
were silently missing.
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
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY on fixture setup AND teardown:
    before, so a sibling plugin's cached copy cannot answer our import; after,
    so nothing generic is left cached for anyone else. The teardown half is
    the load-bearing one -- `loader.import_entry_point` (Task 6's collision
    fix) only evicts modules IT ITSELF introduced, so a name left cached here
    makes a later `find_plugin`/`load_plugin` for gdrive_source or
    microsoft365 silently hand back THIS plugin's code. Verified live.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
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
    discovered = find_plugin("google_workspace", [str(PLUGINS_ROOT)])
    assert discovered is not None, "google_workspace was not discovered at all"
    assert discovered.valid, f"google_workspace manifest is invalid: {discovered.error}"
    assert discovered.manifest is not None
    return parse_manifest(discovered.manifest)


def test_plugin_toml_parses_as_a_valid_manifest() -> None:
    manifest = _manifest()
    assert manifest.name == "google_workspace"
    assert manifest.entry_points["connectors"] == "connector.connector:register"
    assert manifest.tool_pack is not None
    assert len(manifest.tool_pack.connections) == 1
    assert len(manifest.tool_pack.connections[0].guardrail_presets) == 5


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
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    assert conn.config.get("outward_tools") == [
        "gmail_send",
        "gmail_reply",
        "calendar_respond_to_invite",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_create_meet_link",
    ]
    assert isinstance(conn.scopes, dict)
    assert "outward_tools" not in conn.scopes


def test_create_tools_exempt_calendar_inserts_from_read_before_write() -> None:
    """Provisional start-keyed creates must not require a prior read of the event."""
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    assert conn.config.get("create_tools") == [
        "calendar_create_event",
        "calendar_create_meet_link",
    ]


def test_the_bridge_launch_command_matches_the_package_on_disk() -> None:
    """`tool_pack.toml` and `setup/mcp.toml` both launch the SAME bridge, and
    the module they name has to be the package that actually exists -- a stale
    `-m google_workspace_mcp` here fails only at container runtime, where
    nothing in this suite would ever see it."""
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    assert conn.config["args"] == ["-m", "mcp_bridge"]
    assert conn.config["env"]["PYTHONPATH"] == "/app/capas/google_workspace"
    assert manifest.setup is not None
    assert manifest.setup.mcp is not None
    assert manifest.setup.mcp.args == conn.config["args"]
    assert (PLUGIN_ROOT / "mcp_bridge" / "__main__.py").is_file()


def test_every_event_writing_outward_tool_is_scoped_by_the_focus_spec() -> None:
    """An `outward_tools` entry with nothing for `outward_target` to key on is
    capped at ONE successful call per task. That is right for a broadcast and
    wrong for a calendar: `events.insert` writes one event per request and has
    no batch form, so a task could book exactly one meeting, ever. `focus_spec`
    is what turns "once per task" into "once per event" -- assert the two lists
    cannot drift apart, in either direction."""
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    focus = conn.config.get("focus_spec") or {}
    entities = focus.get("tool_entities") or {}
    outward = set(conn.config.get("outward_tools", []))
    event_writers = {t for t in outward if t.startswith("calendar_") and "invite" not in t}
    assert set(entities) == event_writers, (
        f"outward calendar tools without a focus_spec entity: {event_writers - set(entities)}"
    )
    # `eventId` first, so an UPDATE keys on the real event; `start` is the
    # fallback for a CREATE, which has no id until the event exists.
    assert focus.get("id_fields") == ["eventId", "start"]


def test_the_internal_only_preset_grants_no_tool_that_reaches_an_outsider() -> None:
    """The whole-branch review's I5. `internal_only`'s summary promises
    "nothing goes outward", and an operator picks it precisely because of that
    promise -- so the preset must not grant anything the manifest itself
    classifies as reaching a person. Adding an attendee to a Calendar event
    puts that event on the attendee's calendar (including attendees at other
    companies) regardless of `sendUpdates`, which is why the three event-writing
    tools joined `outward_tools`; this asserts the two lists cannot drift back
    apart silently, in either direction."""
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    outward = set(conn.config.get("outward_tools", []))
    preset = next(p for p in conn.guardrail_presets if p.key == "internal_only")
    leaking = outward & set(preset.only)
    assert not leaking, f"internal_only grants outward-reaching tools: {sorted(leaking)}"


def test_read_and_send_scopes_are_disjoint() -> None:
    """A tool in both lists is classified twice and the two answers can
    disagree -- cheap to assert, and exactly what a later tool addition breaks
    without anyone noticing."""
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    assert isinstance(conn.scopes, dict)
    overlap = set(conn.scopes.get("read", [])) & set(conn.scopes.get("modify", []))
    assert not overlap, f"tools classified as both read and send: {sorted(overlap)}"


def test_every_tool_stays_reachable_through_at_least_one_preset_and_one_guardrail() -> None:
    """Narrowing a preset's `only` list (as I5's fix does to `internal_only`)
    must not strand a tool with no path to it at all. A preset or guardrail
    with an EMPTY `only` grants everything, so it makes every tool reachable."""
    from mcp_bridge.__main__ import ALL_TOOLS

    actual = {t.name for t in ALL_TOOLS}
    manifest = _manifest()
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]

    via_presets: set[str] = set()
    for preset in conn.guardrail_presets:
        via_presets |= actual if not preset.only else set(preset.only)
    assert actual - via_presets == set(), f"unreachable by every preset: {actual - via_presets}"

    discovered = find_plugin("google_workspace", [str(PLUGINS_ROOT)])
    assert discovered is not None and discovered.guardrail_library is not None
    via_guardrails: set[str] = set()
    for entry in discovered.guardrail_library.guardrail:
        via_guardrails |= actual if not entry.only else set(entry.only)
    assert actual - via_guardrails == set(), (
        f"unreachable by every guardrails/ library entry: {actual - via_guardrails}"
    )


def test_the_setup_form_survived_the_split_intact() -> None:
    manifest = _manifest()
    assert manifest.setup is not None
    assert manifest.setup.title == "Connect Google Workspace"
    assert [f.key for f in manifest.setup.fields] == [
        "service_account_key",
        "shared_drive_ids",
        "department",
        "delegated_mailboxes",
        "default_mailbox",
    ]
    assert manifest.setup.mcp is not None
    assert manifest.setup.mcp.env_fields == {"GOOGLE_DEFAULT_MAILBOX": "default_mailbox"}


def test_the_any_of_validation_block_survived_the_split() -> None:
    """`setup/validation.toml` is the file microsoft365 has no equivalent of --
    this plugin is the first to exercise `_read_setup_folder`'s optional
    `validation.toml` branch at all. Its absence is NOT an error (that is the
    whole point of "individually optional"), so nothing but this assertion
    stands between a deleted file and a setup form that silently accepts an
    empty/empty submission: no Shared Drive, no delegated mailbox, nothing
    provable, and 31 tools that would all 403/404."""
    manifest = _manifest()
    assert manifest.setup is not None
    assert manifest.setup.validation is not None
    assert manifest.setup.validation.any_of == [["shared_drive_ids"], ["delegated_mailboxes"]]
    field_keys = {f.key for f in manifest.setup.fields}
    for group in manifest.setup.validation.any_of:
        for name in group:
            assert name in field_keys, f"validation names a field the form does not have: {name}"


def test_the_delegated_identity_oauth_provisioning_fields_survived_the_split() -> None:
    """The second path microsoft365 does not exercise: its own
    `oauth_provision.toml` leaves all four delegated-identity fields at their
    empty-string defaults, so this plugin is the first real proof they survive
    `setup/oauth_provision.toml` assembly.

    `delegated_scope` in particular is asserted BYTE FOR BYTE against
    `oc8.oauth.provisioning._DELEGATED_SCOPE`: Google's domain-wide-delegation
    check is scope-exact, so a drift between the two makes a
    correctly-configured tenant fail its consent probe with "delegation not
    authorized" and nothing anywhere pointing at the real cause."""
    from oc8.oauth.provisioning import _DELEGATED_SCOPE

    manifest = _manifest()
    assert manifest.setup is not None
    prov = manifest.setup.oauth_provision
    assert prov is not None
    assert prov.provider == "google"
    assert prov.connector_type == "google_workspace_files"
    assert prov.site_ids_field == "shared_drive_ids"
    assert prov.source_ids_config_key == "sharedDriveIds"
    assert prov.delegated_identities_field == "delegated_mailboxes"
    assert prov.delegated_identities_env == "GOOGLE_DELEGATED_MAILBOXES"
    assert prov.delegated_token_env_prefix == "GOOGLE_DELEGATED_TOKEN"
    assert prov.delegated_scope == _DELEGATED_SCOPE
    field_keys = {f.key for f in manifest.setup.fields}
    assert prov.site_ids_field in field_keys
    assert prov.delegated_identities_field in field_keys


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

    discovered = find_plugin("google_workspace", [str(PLUGINS_ROOT)])
    assert discovered is not None and discovered.guardrail_library is not None
    assert {g.key for g in discovered.guardrail_library.guardrail} == {
        "mail_assistant_drafts_only",
        "mail_and_calendar_only",
        "shared_drive_knowledge_worker_read_only",
        "full_assistant_with_send_approval",
        "docs_editor_no_mail_no_calendar",
    }


def test_the_guardrail_library_parses_through_the_real_validating_parser() -> None:
    """`parse_guardrail_entry` (`oc8/plugins/guardrails.py`) requires `use_case`
    on every entry with no default -- a raw `tomllib.loads` read (as an earlier
    draft of this test did) bypasses that validation entirely and would let a
    library file missing a required field pass this test while `find_plugin`
    marks the WHOLE PLUGIN invalid in production. Go through the real parser,
    the same one discovery.py actually calls."""
    # Default `plugins_path` ("plugins", relative to cwd) does not resolve to
    # the repo's real plugins/ dir when pytest runs from backend/ -- same
    # reasoning as test_odoo_mcp_manifest.py's own explicit `paths` argument.
    discovered = find_plugin("google_workspace", [str(PLUGINS_ROOT)])
    error = discovered.error if discovered else "not found"
    assert discovered is not None and discovered.valid, error
    assert discovered.guardrail_library is not None
    assert len(discovered.guardrail_library.guardrail) == 5


def test_guardrail_only_lists_reference_real_tool_names() -> None:
    """Covers BOTH kinds now that presets and the curated library live side by
    side in `guardrails/` -- before the split only the library file was checked
    through this route, and a preset's `only` list could name a tool that does
    not exist."""
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
