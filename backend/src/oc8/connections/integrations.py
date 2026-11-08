"""Which installed plugins can be connected, and how (connections design).

Derived from the plugin's own manifest, never from a list of vendors: a tool
pack whose setup provisions a Microsoft or Google OAuth connection is an
"oauth" integration (a person signs in); one whose connection names a
credential_type is a "credential" integration (a short form). The tool key is
the tool pack connection's `name` -- the key agents' tool policies and the
runtime's McpConnection rows use.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import Manifest, ManifestError, parse_manifest

#: Providers a person can sign in with to connect their own account.
PERSONAL_OAUTH_PROVIDERS: Final = frozenset({"microsoft", "google"})


@dataclass(frozen=True)
class Integration:
    name: str
    label: str
    summary: str
    tool_key: str
    connection_key: str
    kind: str  # "oauth" | "credential"
    provider: str | None
    credential_type: str | None
    credential_env_fields: dict[str, str]


@dataclass(frozen=True)
class InstalledIntegration:
    integration: Integration
    capa_id: uuid.UUID
    #: Enabled: its tools exist. A legacy never-enabled install is listed with
    #: False so an admin can make it available with one click.
    available: bool


def integration_from_manifest(manifest: Manifest) -> Integration | None:
    pack = manifest.tool_pack
    if pack is None or not pack.connections:
        return None
    conn = pack.connections[0]
    provision = manifest.setup.oauth_provision if manifest.setup is not None else None
    provider = provision.provider if provision is not None else None
    if provider in PERSONAL_OAUTH_PROVIDERS:
        kind, credential_type = "oauth", None
    elif conn.credential_type:
        kind, credential_type, provider = "credential", conn.credential_type, None
    else:
        return None
    return Integration(
        name=manifest.name,
        label=manifest.label or manifest.name,
        summary=manifest.summary,
        tool_key=conn.name,
        connection_key=conn.key,
        kind=kind,
        provider=provider,
        credential_type=credential_type,
        credential_env_fields=dict(conn.credential_env_fields),
    )


async def installed_integrations(db: AsyncSession) -> list[InstalledIntegration]:
    """Every connectable plugin installed in this tenant (RLS-bound session)."""
    rows = (
        await db.execute(
            select(m.Capa, m.CapaInstallation, m.CapaVersion)
            .join(m.CapaInstallation, m.CapaInstallation.capa_id == m.Capa.id)
            .join(m.CapaVersion, m.CapaVersion.id == m.Capa.current_version_id)
            .where(m.CapaInstallation.status.in_(("installed", "enabled")))
        )
    ).all()
    out: list[InstalledIntegration] = []
    for capa, installation, version in rows:
        try:
            manifest = parse_manifest(version.manifest)
        except ManifestError:
            continue
        integration = integration_from_manifest(manifest)
        if integration is not None:
            out.append(InstalledIntegration(integration, capa.id, installation.status == "enabled"))
    return sorted(out, key=lambda item: item.integration.label.lower())


def integration_for_connection(conn: m.McpConnection) -> Integration | None:
    """The integration a materialised McpConnection belongs to, from the
    plugin on disk (the same source `resolve_tool_pack_connection` reads)."""
    cfg = conn.config if isinstance(conn.config, dict) else {}
    plugin = str(cfg.get("_plugin_name", ""))
    if not plugin:
        return None
    found = find_plugin(plugin)
    if found is None or not found.valid or found.manifest is None:
        return None
    try:
        manifest = parse_manifest(found.manifest)
    except ManifestError:
        return None
    integration = integration_from_manifest(manifest)
    if integration is None or integration.tool_key != conn.name:
        return None
    return integration
