"""A connection's read/modify classification held on its own row.

A connection whose plugin ships a classification is classified by that
manifest (`capas.discovery.resolve_tool_pack_connection`). Every other one --
the tenant's own plugin, or a connection set up by hand -- keeps the
classification a person gave it on the row, as `{"read": [...], "modify":
[...]}`. Anything it does not list requires `modify` (`authz.pdp.required_right`).
"""

from __future__ import annotations

from typing import Any

from oc8 import models as m


def row_tool_scopes(conn: m.McpConnection) -> dict[str, list[str]] | None:
    """The row's own classification, or None where it holds none (an older
    flat list, or nothing)."""
    scopes = conn.scopes
    if not isinstance(scopes, dict):
        return None
    return {right: _names(scopes.get(right)) for right in ("read", "modify")}


def offered_tool_names(conn: m.McpConnection) -> list[str]:
    """What the server offered at its last successful connection test."""
    health = conn.health if isinstance(conn.health, dict) else {}
    if health.get("status") != "ok":
        return []
    return _names(health.get("tools"))


def known_tool_names(conn: m.McpConnection) -> list[str]:
    """Every tool name this row knows: offered by the server, or classified."""
    scopes = row_tool_scopes(conn) or {}
    return sorted({*offered_tool_names(conn), *scopes.get("read", []), *scopes.get("modify", [])})


def classified(read: list[str], modify: list[str]) -> dict[str, list[str]]:
    """Deduplicated and sorted; a name in both lists is the more dangerous one."""
    modify_set = set(modify)
    return {
        "read": sorted(set(read) - modify_set),
        "modify": sorted(modify_set),
    }


def _names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({str(v) for v in value if isinstance(v, str) and v})
