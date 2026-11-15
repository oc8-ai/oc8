"""Connector lookup (§11.2).

Two doors, on purpose:

* ``get_connector`` -- synchronous, **core connectors only**. Every tenant may
  use these, so it needs no tenant and cannot leak anything.
* ``resolve_connector`` -- asynchronous, tenant-scoped, and the only way to
  reach a connector contributed by a plugin.

A plugin's Python module is imported once per process, but a plugin install is
per tenant. So loading a plugin must never, by itself, make its connector usable:
``resolve_connector`` first establishes that *this* tenant has the plugin
installed **and enabled**, and only then looks at what that plugin contributed.
The reverse shape -- a flat process-global dict of usable connectors -- is the
one that produced this project's cross-tenant hook-registry Critical.

Unknown type, plugin not installed, plugin installed but not enabled, and plugin
failed to import all end the same way: ``ConnectorError``. Fail closed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.capas.contributions import connectors_for
from oc8.capas.discovery import find_plugin
from oc8.capas.loader import load_plugin
from oc8.capas.manifest import DEFERRABLE_PERMISSIONS
from oc8.knowledge.connectors.base import Connector, ConnectorError
from oc8.knowledge.connectors.upload import _UPLOAD
from oc8.knowledge.connectors.website import _WEBSITE
from oc8.models import Capa, CapaInstallation, CapaVersion

_CONNECTORS: dict[str, Connector] = {c.type_id: c for c in (_UPLOAD, _WEBSITE)}


def get_connector(type_id: str) -> Connector:
    """A built-in connector. Never returns a plugin contribution -- use
    ``resolve_connector`` for those, so the tenant check cannot be skipped."""
    try:
        return _CONNECTORS[type_id]
    except KeyError:
        raise ConnectorError(f"unknown connector type: {type_id!r}") from None


def connector_types() -> list[str]:
    """Built-in connector types only. See ``available_connector_types``."""
    return list(_CONNECTORS)


async def enabled_plugin_names(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """Names of the plugins this tenant has installed AND enabled.

    Enabled, not merely installed: ``install_plugin`` records no
    ``CapaInstallation`` at all, and ``enable_plugin`` is where the operator
    grants the plugin's declared permissions. Contributing executable code on
    the strength of an install alone would skip that consent step.

    Public (not underscore-prefixed): ``oc8.credentials.registry`` reuses this
    exact "which plugins may this tenant's contributions come from" check
    verbatim, rather than reimplementing it -- see that module's own
    docstring.
    """
    rows = (
        await db.execute(
            select(Capa.name)
            .join(CapaInstallation, CapaInstallation.capa_id == Capa.id)
            .where(
                Capa.tenant_id == tenant_id,
                CapaInstallation.tenant_id == tenant_id,
                CapaInstallation.status == "enabled",
            )
        )
    ).scalars()
    return list(rows)


#: The permission a capa's knowledge contributions need. A capa may defer it
#: (``Manifest.deferred_permissions``); everything it contributes to
#: knowledge -- connectors, vector indexes -- then waits for the grant.
KNOWLEDGE_WRITE = "knowledge:write"


async def _enabled_plugin_grants(
    db: AsyncSession, tenant_id: uuid.UUID
) -> list[tuple[str, uuid.UUID, set[str], set[str]]]:
    """(name, capa id, granted permissions, deferred permissions) of each plugin
    this tenant has enabled -- the same rows as ``enabled_plugin_names``, read
    with what the enabled version defers and what the tenant granted."""
    rows = await db.execute(
        select(
            Capa.name,
            Capa.id,
            CapaInstallation.granted_permissions,
            CapaVersion.manifest["deferred_permissions"],
        )
        .join(CapaInstallation, CapaInstallation.capa_id == Capa.id)
        .outerjoin(CapaVersion, CapaVersion.id == CapaInstallation.version_id)
        .where(
            Capa.tenant_id == tenant_id,
            CapaInstallation.tenant_id == tenant_id,
            CapaInstallation.status == "enabled",
        )
    )
    return [
        # Only allow-listed permissions count as deferred, whatever the
        # stored manifest says (see DEFERRABLE_PERMISSIONS).
        (name, capa_id, set(granted or []), set(deferred or []) & DEFERRABLE_PERMISSIONS)
        for name, capa_id, granted, deferred in rows.all()
    ]


async def knowledge_plugin_gate(
    db: AsyncSession, tenant_id: uuid.UUID
) -> tuple[list[str], list[tuple[str, uuid.UUID]]]:
    """The one gate for knowledge contributions from enabled plugins.

    Returns (names whose knowledge contributions this tenant may use, and
    (name, capa id) of those waiting for their deferred ``knowledge:write``).
    Every registry that resolves a plugin's knowledge contribution -- the
    connectors here, ``oc8.knowledge.vector_indexes.registry`` -- goes
    through this, so none of them can skip the consent.
    """
    usable: list[str] = []
    pending: list[tuple[str, uuid.UUID]] = []
    for name, capa_id, granted, deferred in await _enabled_plugin_grants(db, tenant_id):
        if KNOWLEDGE_WRITE in deferred and KNOWLEDGE_WRITE not in granted:
            pending.append((name, capa_id))
        else:
            usable.append(name)
    return usable, pending


def load_contributing_plugin(name: str) -> bool:
    """Whether plugin ``name`` is on disk and loaded, so its contributions can
    be read. Missing or failing to load is not an error -- it contributes
    nothing."""
    discovered = find_plugin(name)
    return discovered is not None and load_plugin(discovered)


async def _plugin_connectors(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Connector]:
    """Connectors contributed by the plugins THIS tenant may use.

    A plugin missing from disk, quarantined, or contributing nothing simply
    contributes nothing here -- none of those is an error for the caller. Nor
    does a plugin that defers ``knowledge:write`` and has not been granted it.
    """
    out: dict[str, Connector] = {}
    usable, _pending = await knowledge_plugin_gate(db, tenant_id)
    for name in usable:
        if load_contributing_plugin(name):
            out.update(connectors_for(name))
    return out


async def consent_pending_connectors(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> dict[str, tuple[Connector, uuid.UUID, str]]:
    """Connectors an enabled plugin would contribute once its deferred
    ``knowledge:write`` is granted: type id -> (connector, capa id, permission).

    For the catalog only, so it can offer the consent step. Never resolvable:
    ``resolve_connector`` does not look here.
    """
    out: dict[str, tuple[Connector, uuid.UUID, str]] = {}
    _usable, pending = await knowledge_plugin_gate(db, tenant_id)
    for name, capa_id in pending:
        if not load_contributing_plugin(name):
            continue
        for type_id, connector in connectors_for(name).items():
            out[type_id] = (connector, capa_id, KNOWLEDGE_WRITE)
    return out


async def resolve_connector(db: AsyncSession, *, tenant_id: uuid.UUID, type_id: str) -> Connector:
    """The tenant-scoped lookup. Core connectors first (no DB work); otherwise
    only what this tenant's enabled plugins contribute."""
    core = _CONNECTORS.get(type_id)
    if core is not None:
        return core
    contributed = await _plugin_connectors(db, tenant_id)
    connector = contributed.get(type_id)
    if connector is None:
        raise ConnectorError(f"unknown connector type: {type_id!r}")
    return connector


async def available_connector_types(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[str]:
    """Every connector type this tenant may actually use, built-in plus plugin."""
    return sorted(set(_CONNECTORS) | set(await _plugin_connectors(db, tenant_id)))


async def available_connectors(db: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, Connector]:
    """Connector instances usable by this tenant, including enabled plugins.

    This is deliberately tenant-scoped: it is the catalog used by the UI, so it
    must expose neither merely-installed nor another tenant's connectors.
    """
    return {**_CONNECTORS, **(await _plugin_connectors(db, tenant_id))}
