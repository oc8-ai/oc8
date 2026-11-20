"""A capa declares its events either as plain names or as tables that say how
the event is shown and whether core polls for it."""

from __future__ import annotations

import pytest

from oc8.automation.catalogue import _descriptor_from_manifest
from oc8.capas.manifest import ManifestError, TriggerDecl, parse_manifest


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
