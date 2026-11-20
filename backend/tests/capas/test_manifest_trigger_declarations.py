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
    manifest = parse_manifest(
        _with_config(
            {"key": "folder", "label": "Folder", "default": "INBOX"},
            {"key": "user", "label": "Mailbox", "default_from": "default_user"},
        )
    )

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
