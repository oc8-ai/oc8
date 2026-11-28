"""Usage limits name real tools: the check every write path shares.

A limit on a tool the connection does not have counts nothing -- most likely
a typo ("mail_sned"), and one that quietly leaves the real tool unlimited. The
department tools endpoint, the agent narrowing endpoints and the Copilot's
narrowing operation therefore refuse one, against the same manifest catalog
the guardrail editor offers (`resolve_tool_pack_connection`).

A connection whose catalog is unknown (no manifest behind it) is not judged:
there is nothing to compare against, and refusing every limit on it would
make limits unusable there rather than safer.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.connections.tool_connection import not_retired
from oc8.connections.tool_scopes import known_tool_names


def connection_tool_names(conn: m.McpConnection) -> frozenset[str]:
    """The manifest-declared function catalog of `conn`, or -- without one --
    the tools its row knows of; empty when neither is known."""
    cfg = conn.config if isinstance(conn.config, dict) else {}
    plugin_name = cfg.get("_plugin_name")
    connection_key = cfg.get("_connection_key")
    manifest_conn = (
        resolve_tool_pack_connection(plugin_name, connection_key)
        if isinstance(plugin_name, str) and isinstance(connection_key, str)
        else None
    )
    if manifest_conn is None or not isinstance(manifest_conn.scopes, dict):
        # No shipped classification: the tools the row knows of
        # (`connections.tool_scopes`), so a limit may name them.
        return frozenset(known_tool_names(conn))
    scopes = manifest_conn.scopes
    return frozenset({*scopes.get("read", []), *scopes.get("modify", [])})


async def unknown_limit_tools(
    db: AsyncSession, *, tenant_id: uuid.UUID, tools: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """One violation per tool key whose limits name tools its connection does
    not have: `{"connection": key, "unknown_tools": [...]}`."""
    violations: list[dict[str, Any]] = []
    for key, raw in tools.items():
        limits = raw.get("limits") if isinstance(raw, Mapping) else None
        if not isinstance(limits, list) or not limits:
            continue
        # `credential_id.is_(None)` picks the manifest row, never a per-login
        # row sharing the same tenant-global name.
        conn = (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.tenant_id == tenant_id,
                    m.McpConnection.name == key,
                    m.McpConnection.credential_id.is_(None),
                    not_retired(),
                )
                .order_by(m.McpConnection.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        known = connection_tool_names(conn) if conn is not None else frozenset()
        if not known:
            continue
        named = {
            str(tool)
            for lim in limits
            if isinstance(lim, Mapping) and isinstance(lim.get("tools"), list)
            for tool in lim["tools"]
        }
        unknown = sorted(named - known)
        if unknown:
            violations.append({"connection": str(key), "unknown_tools": unknown})
    return violations
