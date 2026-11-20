"""Value-safe catalogue of the events an automation may subscribe to.

Descriptors are deliberately display metadata only.  Secrets and connection
configuration stay behind their own services and never enter this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class AutomationEventDescriptor:
    source: str
    type: str
    label: str
    description: str = ""
    #: Core polls an outside system for this event (`oc8.events.sources`);
    #: the trigger editor offers it as its own tab.
    polled: bool = False


class DuplicateAutomationEvent(ValueError):
    """Raised when two catalogue entries claim the same source/type pair."""


_BUILTIN_EVENTS: tuple[AutomationEventDescriptor, ...] = (
    AutomationEventDescriptor(
        source="core",
        type="signal.received",
        label="Signal received",
        description="Run when a generic system signal is received.",
    ),
)


def list_automation_events(
    *, plugin_descriptors: Iterable[AutomationEventDescriptor] = ()
) -> list[AutomationEventDescriptor]:
    """Return built-in neutral events plus descriptors from enabled plugins.

    An identifier is a stable ``(source, type)`` pair, so a collision is an
    error rather than an arbitrary load-order decision.
    """
    events = [*_BUILTIN_EVENTS, *plugin_descriptors]
    identifiers = [(event.source, event.type) for event in events]
    if len(identifiers) != len(set(identifiers)):
        raise DuplicateAutomationEvent("duplicate automation event source/type")
    return events


def _descriptor_from_manifest(plugin_name: str, value: Any) -> AutomationEventDescriptor | None:
    """Read only public event metadata from a plugin manifest entry."""
    if isinstance(value, str) and value:
        return AutomationEventDescriptor(source=plugin_name, type=value, label=value)
    if not isinstance(value, dict):
        return None
    event_type = value.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None
    source = value.get("source", plugin_name)
    label = value.get("label", event_type)
    description = value.get("description", "")
    polled = value.get("polled", False)
    if not all(isinstance(item, str) for item in (source, label, description)):
        return None
    if not isinstance(polled, bool):
        return None
    return AutomationEventDescriptor(
        source=source,
        type=event_type,
        label=label or event_type,
        description=description,
        polled=polled,
    )


async def installed_plugin_event_descriptors(db: AsyncSession) -> list[AutomationEventDescriptor]:
    """Read descriptor metadata from this tenant's enabled plugin versions."""
    from oc8.models import Capa, CapaInstallation, CapaVersion

    rows = (
        await db.execute(
            select(Capa.name, CapaVersion.manifest)
            .join(CapaInstallation, CapaInstallation.capa_id == Capa.id)
            .join(CapaVersion, CapaInstallation.version_id == CapaVersion.id)
            .where(CapaInstallation.status == "enabled")
        )
    ).all()
    descriptors: list[AutomationEventDescriptor] = []
    for plugin_name, manifest in rows:
        triggers = (manifest or {}).get("triggers", [])
        if not isinstance(triggers, list):
            continue
        descriptors.extend(
            descriptor
            for value in triggers
            if (descriptor := _descriptor_from_manifest(plugin_name, value)) is not None
        )
    return descriptors


async def list_installed_automation_events(db: AsyncSession) -> list[AutomationEventDescriptor]:
    return list_automation_events(plugin_descriptors=await installed_plugin_event_descriptors(db))
