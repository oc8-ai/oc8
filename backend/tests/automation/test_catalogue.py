from __future__ import annotations

from dataclasses import fields

import pytest

from oc8.automation.catalogue import (
    AutomationEventConfigField,
    AutomationEventDescriptor,
    DuplicateAutomationEvent,
    list_automation_events,
)


def test_event_descriptors_are_value_safe_metadata_only() -> None:
    """The automation catalogue is suitable for UI/model context, never secrets."""
    assert [field.name for field in fields(AutomationEventDescriptor)] == [
        "source",
        "type",
        "label",
        "description",
        "polled",
        "config",
        "editor_label",
    ]
    # A setting declares how it is shown and filled, never a value.
    assert [field.name for field in fields(AutomationEventConfigField)] == [
        "key",
        "label",
        "kind",
        "default",
        "default_from",
    ]


def test_catalogue_combines_neutral_and_plugin_events() -> None:
    plugin_event = AutomationEventDescriptor(
        source="plugin.example", type="record.updated", label="Record updated"
    )

    events = list_automation_events(plugin_descriptors=[plugin_event])

    assert plugin_event in events
    assert any(event.source == "core" for event in events)


def test_catalogue_rejects_duplicate_source_and_type() -> None:
    event = AutomationEventDescriptor(source="plugin.example", type="record.updated", label="x")

    with pytest.raises(DuplicateAutomationEvent):
        list_automation_events(plugin_descriptors=[event, event])
