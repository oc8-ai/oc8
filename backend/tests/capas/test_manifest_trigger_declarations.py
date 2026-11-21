"""A capa declares its events either as plain names or as tables that say how
the event is shown and whether core polls for it."""

from __future__ import annotations

import pytest

from oc8.automation.catalogue import AutomationEventConfigField, _descriptor_from_manifest
from oc8.capas.manifest import ManifestError, TriggerConfigField, TriggerDecl, parse_manifest


def test_a_plain_name_keeps_its_meaning() -> None:
    manifest = parse_manifest({"name": "p", "version": "1.0.0", "triggers": ["issue.opened"]})

    assert manifest.triggers == ["issue.opened"]


def test_a_table_declares_label_description_and_polling() -> None:
    manifest = parse_manifest(
        {
            "name": "p",
            "version": "1.0.0",
            "triggers": [
                "issue.opened",
                {
                    "type": "item.arrived",
                    "label": "New item",
                    "description": "Runs when an item arrives.",
                    "polled": True,
                },
            ],
        }
    )

    assert manifest.triggers[0] == "issue.opened"
    assert manifest.triggers[1] == TriggerDecl(
        type="item.arrived",
        label="New item",
        description="Runs when an item arrives.",
        polled=True,
    )


def test_a_table_needs_a_type_and_nothing_unknown() -> None:
    with pytest.raises(ManifestError):
        parse_manifest({"name": "p", "version": "1.0.0", "triggers": [{"label": "x"}]})
    with pytest.raises(ManifestError):
        parse_manifest(
            {"name": "p", "version": "1.0.0", "triggers": [{"type": "x", "every": "1m"}]}
        )


def test_a_table_defaults_to_not_polled() -> None:
    manifest = parse_manifest({"name": "p", "version": "1.0.0", "triggers": [{"type": "x"}]})

    assert manifest.triggers == [TriggerDecl(type="x")]
    assert TriggerDecl(type="x").polled is False


def test_the_catalogue_exposes_polled() -> None:
    manifest = parse_manifest(
        {
            "name": "p",
            "version": "1.0.0",
            "triggers": ["plain", {"type": "item.arrived", "label": "New item", "polled": True}],
        }
    )
    stored = manifest.model_dump()["triggers"]

    plain = _descriptor_from_manifest("p", stored[0])
    polled = _descriptor_from_manifest("p", stored[1])

    assert plain is not None and plain.polled is False
    assert polled is not None
    assert (polled.source, polled.type, polled.label, polled.polled) == (
        "p",
        "item.arrived",
        "New item",
        True,
    )


def _with_config(*fields: dict[str, object]) -> dict[str, object]:
    return {
        "name": "p",
        "version": "1.0.0",
        "triggers": [
            {"type": "item.arrived", "label": "New item", "polled": True, "config": list(fields)}
        ],
    }


def test_a_table_declares_its_trigger_settings() -> None:
    raw = _with_config(
        {"key": "folder", "label": "Folder", "default": "INBOX"},
        {"key": "user", "label": "Mailbox", "default_from": "default_user"},
    )
    raw["setup"] = {
        "title": "Set up",
        "fields": [{"key": "default_user", "label": "Default", "kind": "text"}],
        "mcp": {"name": "tools", "env_fields": {"DEFAULT_USER": "default_user"}},
    }
    manifest = parse_manifest(raw)

    decl = manifest.triggers[0]
    assert isinstance(decl, TriggerDecl)
    assert decl.config == [
        TriggerConfigField(key="folder", label="Folder", default="INBOX"),
        TriggerConfigField(key="user", label="Mailbox", default_from="default_user"),
    ]
    assert decl.config[0].kind == "text"
    assert TriggerDecl(type="x").config == []


@pytest.mark.parametrize(
    "fields",
    [
        [{"key": "folder", "label": "Folder", "kind": "number"}],
        [{"key": "folder", "label": "Folder", "secret": True}],
        [{"label": "Folder"}],
        [{"key": "", "label": "Folder"}],
        [{"key": "Folder Name", "label": "Folder"}],
        [{"key": "folder", "label": "A"}, {"key": "folder", "label": "B"}],
        [{"key": "folder", "label": "Folder", "default": "x" * 201}],
    ],
)
def test_a_bad_trigger_setting_is_refused(fields: list[dict[str, object]]) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(_with_config(*fields))


def test_the_catalogue_exposes_the_trigger_settings() -> None:
    manifest = parse_manifest(
        _with_config({"key": "folder", "label": "Folder", "default": "INBOX"})
    )
    stored = manifest.model_dump(mode="json")["triggers"]

    descriptor = _descriptor_from_manifest("p", stored[0])

    assert descriptor is not None
    assert descriptor.config == (
        AutomationEventConfigField(key="folder", label="Folder", default="INBOX"),
    )
    plain = _descriptor_from_manifest("p", "plain")
    assert plain is not None and plain.config == ()


def test_the_catalogue_drops_an_event_whose_settings_are_malformed() -> None:
    assert (
        _descriptor_from_manifest(
            "p", {"type": "x", "polled": True, "config": [{"key": 1, "label": "Folder"}]}
        )
        is None
    )


def _defaulted_from(
    field_kind: str, *, mapped: bool = True, secret: bool = False
) -> dict[str, object]:
    manifest = _with_config({"key": "user", "label": "Mailbox", "default_from": "default_user"})
    field: dict[str, object] = {"key": "default_user", "label": "Default", "kind": field_kind}
    if field_kind == "credential":
        field["credential_type"] = "some_login"
    if field_kind == "select":
        field["options"] = ["a"]
    mcp: dict[str, object] = {"name": "tools", "env_fields": {}, "secret_env_fields": {}}
    if mapped:
        mcp["env_fields"] = {"DEFAULT_USER": "default_user"}
    if secret:
        mcp["secret_env_fields"] = {"DEFAULT_USER_SECRET": "default_user"}
    manifest["setup"] = {"title": "Set up", "fields": [field], "mcp": mcp}
    return manifest


@pytest.mark.parametrize("kind", ["text", "url", "select"])
def test_a_setting_may_default_from_a_plain_mapped_setup_field(kind: str) -> None:
    parse_manifest(_defaulted_from(kind))


@pytest.mark.parametrize(
    "manifest",
    [
        _defaulted_from("password"),
        _defaulted_from("credential"),
        _defaulted_from("department"),
        _defaulted_from("text", mapped=False),
        _defaulted_from("text", secret=True),
        _with_config({"key": "user", "label": "Mailbox", "default_from": "default_user"}),
    ],
)
def test_a_setting_never_defaults_from_a_secret_or_unknown_field(
    manifest: dict[str, object],
) -> None:
    with pytest.raises(ManifestError, match="default_from"):
        parse_manifest(manifest)


def test_a_table_may_name_its_trigger_editor_tab() -> None:
    raw = _with_config()
    raw["triggers"][0]["editor_label"] = "On new item"  # type: ignore[index]
    manifest = parse_manifest(raw)

    decl = manifest.triggers[0]
    assert isinstance(decl, TriggerDecl) and decl.editor_label == "On new item"
    assert TriggerDecl(type="x").editor_label == ""
    descriptor = _descriptor_from_manifest("p", manifest.model_dump(mode="json")["triggers"][0])
    assert descriptor is not None and descriptor.editor_label == "On new item"


def _allowed_from(
    *, kind: str = "text", mapped: str = "env", acts_as: bool = False, allowed: bool = True
) -> dict[str, object]:
    setting: dict[str, object] = {"key": "mailbox", "label": "Mailbox"}
    if allowed:
        setting["allowed_from"] = "mailboxes"
    if acts_as:
        setting["acts_as"] = True
    manifest = _with_config(setting)
    field: dict[str, object] = {"key": "mailboxes", "label": "Mailboxes", "kind": kind}
    if kind == "password":
        field = {"key": "mailboxes", "label": "Mailboxes", "kind": "password"}
    mcp: dict[str, object] = {"name": "tools", "env_fields": {}}
    setup: dict[str, object] = {"title": "Set up", "fields": [field], "mcp": mcp}
    if mapped == "env":
        mcp["env_fields"] = {"MAILBOXES": "mailboxes"}
    elif mapped == "delegated":
        # Kept in the connection's env by the OAuth provisioning instead.
        setup["oauth_provision"] = {
            "provider": "google",
            "delegated_identities_field": "mailboxes",
            "delegated_identities_env": "MAILBOXES",
            "delegated_token_env_prefix": "TOKEN",
            "delegated_scope": "s",
        }
    manifest["setup"] = setup
    return manifest


@pytest.mark.parametrize("mapped", ["env", "delegated"])
def test_a_setting_may_be_limited_to_a_plain_setup_list(mapped: str) -> None:
    manifest = parse_manifest(_allowed_from(mapped=mapped, acts_as=True))
    decl = manifest.triggers[0]
    assert isinstance(decl, TriggerDecl)
    assert decl.config[0].allowed_from == "mailboxes" and decl.config[0].acts_as


@pytest.mark.parametrize(
    "manifest",
    [_allowed_from(kind="password"), _allowed_from(mapped="none")],
)
def test_a_setting_is_never_limited_by_a_secret_or_unkept_field(
    manifest: dict[str, object],
) -> None:
    with pytest.raises(ManifestError, match="allowed_from"):
        parse_manifest(manifest)


def test_a_setting_the_login_acts_as_must_be_limited() -> None:
    with pytest.raises(ManifestError, match="acts_as"):
        parse_manifest(_allowed_from(allowed=False, acts_as=True))


def test_only_one_setting_is_acted_as() -> None:
    manifest = _allowed_from(acts_as=True)
    config = manifest["triggers"][0]["config"]  # type: ignore[index]
    config.append(dict(config[0], key="other"))
    with pytest.raises(ManifestError, match="acts_as"):
        parse_manifest(manifest)
