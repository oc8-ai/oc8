"""The email capa as core discovers and loads it: the manifest assembled from
its split files, the polled `mail.received` event and the poller registered
for it, the login type, the presets and the German catalogue."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import polib

from oc8.capas.contributions import pollers_for
from oc8.capas.discovery import find_plugin
from oc8.capas.loader import load_plugin
from oc8.capas.manifest import Manifest, TriggerDecl, parse_manifest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CAPAS_ROOT = PLUGIN_ROOT.parent


def _manifest() -> Manifest:
    discovered = find_plugin("email", [str(CAPAS_ROOT)])
    assert discovered is not None, "email was not discovered"
    assert discovered.valid, f"email manifest is invalid: {discovered.error}"
    assert discovered.manifest is not None
    return parse_manifest(discovered.manifest)


def _toml(relative: str) -> dict[str, Any]:
    return tomllib.loads((PLUGIN_ROOT / relative).read_text())


def test_the_capa_is_a_tool_pack_at_version_one() -> None:
    manifest = _manifest()
    assert manifest.name == "email"
    assert manifest.version == "1.0.0"
    assert manifest.type == "tool_pack"
    assert manifest.entry_points == {"event_sources": "events.source:register"}


def test_it_declares_mail_received_as_a_polled_event() -> None:
    manifest = _manifest()
    assert manifest.triggers == [
        TriggerDecl(
            type="mail.received",
            label="New email",
            description=manifest.triggers[0].description,  # type: ignore[union-attr]
            polled=True,
        )
    ]
    decl = manifest.triggers[0]
    assert isinstance(decl, TriggerDecl) and decl.description


def test_loading_the_capa_registers_the_poller_for_mail_accounts() -> None:
    discovered = find_plugin("email", [str(CAPAS_ROOT)])
    assert discovered is not None
    assert load_plugin(discovered)
    registered = pollers_for("email")["mail.received"]
    assert registered.credential_type == "mail_account"
    assert callable(registered.poller)


def test_the_tool_key_is_email_and_the_login_type_is_mail_account() -> None:
    from oc8.connections.integrations import integration_from_manifest

    manifest = _manifest()
    integration = integration_from_manifest(manifest)
    assert integration is not None and integration.tool_key == "email"
    assert manifest.tool_pack is not None
    conn = manifest.tool_pack.connections[0]
    assert conn.name == "email"
    assert conn.credential_type == "mail_account"
    assert conn.transport == "stdio"
    assert conn.config["command"] == "python"
    assert conn.config["args"] == ["-m", "mcp_bridge"]
    assert conn.config["env"]["PYTHONPATH"] == "/app/capas/email"


def test_scopes_cover_every_tool_and_nothing_else(tools: ModuleType) -> None:
    manifest = _manifest()
    assert manifest.tool_pack is not None
    scopes = manifest.tool_pack.connections[0].scopes
    assert isinstance(scopes, dict)
    assert scopes["read"] == ["mail_list_folders", "mail_search", "mail_get", "mail_get_attachment"]
    assert scopes["modify"] == [
        "mail_send",
        "mail_reply",
        "mail_forward",
        "mail_move",
        "mail_mark_read",
        "mail_delete",
    ]
    declared = set(scopes["read"]) | set(scopes["modify"])
    assert declared == {t.name for t in tools.TOOLS}
    assert declared == set(tools.CALL_HANDLERS)


def test_sending_tools_are_outward_and_irreversible() -> None:
    manifest = _manifest()
    assert manifest.tool_pack is not None
    config = manifest.tool_pack.connections[0].config
    assert config["outward_tools"] == ["mail_send", "mail_reply", "mail_forward"]
    assert config["irreversible_tools"] == ["mail_send", "mail_reply", "mail_forward"]
    assert config["destructive_tools"] == ["mail_delete"]


def test_the_login_type_has_the_mail_account_fields_and_no_plain_text_option(
    mailbox: ModuleType,
) -> None:
    discovered = find_plugin("email", [str(CAPAS_ROOT)])
    assert discovered is not None and discovered.manifest is not None
    cred_types = {t["name"]: t for t in discovered.manifest["credential_types"]}
    spec = cred_types["mail_account"]
    fields = {f["key"]: f for f in spec["fields"]}
    assert list(fields) == [
        "incoming_protocol",
        "incoming_host",
        "incoming_port",
        "incoming_security",
        "smtp_host",
        "smtp_port",
        "smtp_security",
        "username",
        "password",
        "smtp_username",
        "smtp_password",
        "from_address",
        "from_name",
    ]
    assert fields["incoming_protocol"]["options"] == ["imap", "pop3"]
    assert fields["incoming_security"]["options"] == ["ssl", "starttls"]
    assert fields["smtp_security"]["options"] == ["ssl", "starttls"]
    secret = {k for k, f in fields.items() if f.get("kind") == "password"}
    assert secret == {"password", "smtp_password"}
    optional = {k for k, f in fields.items() if f.get("required") is False}
    assert optional == {"smtp_username", "smtp_password", "from_name"}
    assert spec["validate_entry_point"] == "credential:validate_mail_account"
    # Every field reaches the bridge under the name the bridge reads.
    manifest = _manifest()
    assert manifest.tool_pack is not None
    env_fields = manifest.tool_pack.connections[0].credential_env_fields
    assert env_fields == mailbox.ENV_FIELDS
    assert set(env_fields.values()) == set(fields)


def test_the_setup_only_chooses_the_login() -> None:
    manifest = _manifest()
    assert manifest.setup is not None and manifest.setup.mcp is not None
    login = [f for f in manifest.setup.fields if f.kind == "credential"]
    assert [f.credential_type for f in login] == ["mail_account"]
    assert manifest.setup.mcp.name == "email"
    assert manifest.setup.mcp.command == "python"
    assert manifest.setup.mcp.args == ["-m", "mcp_bridge"]
    assert manifest.setup.mcp.env_fields == {}
    assert manifest.setup.mcp.secret_env_fields == {}


def test_presets_default_to_approval_before_sending() -> None:
    manifest = _manifest()
    assert manifest.tool_pack is not None
    presets = {p.key: p for p in manifest.tool_pack.connections[0].guardrail_presets}
    assert set(presets) == {
        "read_only",
        "send_with_approval",
        "answer_on_its_own",
        "no_deletions",
    }
    assert [k for k, p in presets.items() if p.recommended] == ["send_with_approval"]
    assert presets["read_only"].modify is False
    assert presets["send_with_approval"].approval_actions == ["modify"]
    assert "mail_delete" not in presets["no_deletions"].only
    # Answering on its own reads, replies and marks read; nothing else.
    answer = presets["answer_on_its_own"]
    assert answer.label == "Answer on its own"
    assert answer.approval_actions == []
    assert sorted(answer.only) == sorted(
        [
            "mail_list_folders",
            "mail_search",
            "mail_get",
            "mail_get_attachment",
            "mail_reply",
            "mail_mark_read",
        ]
    )
    assert "no daily" in answer.summary


def test_every_shown_string_has_a_german_translation() -> None:
    catalog = {e.msgid: e.msgstr for e in polib.pofile(str(PLUGIN_ROOT / "i18n" / "de.po"))}
    shown: list[str] = []
    plugin = _toml("plugin.toml")["plugin"]
    shown += [plugin["label"], plugin["summary"]]
    for trigger in plugin["triggers"]:
        shown += [trigger["label"], trigger["description"]]
    cred = _toml("credential_types/mail_account.toml")
    shown.append(cred["display_name"])
    for field in cred["fields"]:
        shown += [field["label"], *([field["help"]] if field.get("help") else [])]
    setup = _toml("setup/fields.toml")
    shown += [setup["title"], setup["description"], setup["submit_label"]]
    for field in setup["fields"]:
        shown += [field["label"], *([field["help"]] if field.get("help") else [])]
    for path in sorted((PLUGIN_ROOT / "guardrails").glob("*.toml")):
        preset = tomllib.loads(path.read_text())
        shown += [preset["label"], preset["summary"]]
    missing = [s for s in shown if not catalog.get(s)]
    assert missing == []
    # German addresses the reader with "du", never "Sie".
    for msgstr in catalog.values():
        assert " Sie " not in f" {msgstr} " and "Ihr" not in msgstr, msgstr
