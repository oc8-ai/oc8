"""An agent's approval threshold on a connection with no value to compare it
against: the check the agent narrowing route and the Copilot's
`agent.narrowing.set` share.

`approval_eur` holds a call above an amount, so it needs the connection's
manifest to say where a record's amount lives (a `value_spec`). On a
connection without one, an agent's own threshold could never hold. The
department's threshold still applies through the frame
(`authz.pdp.effective_tool_policies` takes the frame's value), so the agent's
copy is never needed either.

Such a threshold is therefore dropped from the narrowing that is stored,
rather than refused. Refusing it locked agents out (E2E F3, then its review):
the frame check demands the frame's threshold be kept, the page echoes the
stored one back on every save, and once the department changed its value the
stored copy matched nothing, so every save failed with the field hidden. A
value looser than the frame is still refused, by the same frame check every
other rule goes through.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import SubsetViolation, ToolPolicy, narrowing_within_frame
from oc8.capas.discovery import connection_supports_value_spec, resolve_tool_pack_connection
from oc8.connections.tool_connection import not_retired


@dataclass(frozen=True)
class CheckedNarrowing:
    #: What to store: the narrowing, without thresholds nothing can enforce.
    narrowing: dict[str, Any]
    #: Where it goes beyond the department frame; empty when it fits.
    violations: list[SubsetViolation]


async def has_value_spec(db: AsyncSession, *, tenant_id: uuid.UUID, key: str) -> bool:
    """Whether the connection named `key` declares a value_spec. Unknown (no
    connection, no manifest) counts as without one."""
    # `credential_id.is_(None)` picks the manifest row, never another login
    # sharing this same tenant-global name (`POST /mcp/logins` creates a second
    # row with the same `name`).
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
    cfg: Mapping[str, Any] = {}
    if conn is not None and isinstance(conn.config, dict):
        cfg = conn.config
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    return connection_supports_value_spec(manifest_conn)


async def check_agent_narrowing(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    frame: dict[str, Any],
    narrowing: dict[str, Any],
) -> CheckedNarrowing:
    """Judge `narrowing` against `frame`, with each threshold on a connection
    without a value_spec judged and then dropped.

    The frame check sees such a key's threshold as sent, or the frame's own
    when none was sent: a looser value is still a violation, and leaving it
    out removes nothing, since the frame's applies anyway. Raises ValueError
    for a malformed policy, as `narrowing_within_frame` does."""
    raw_tools = narrowing.get("tools")
    if not isinstance(raw_tools, Mapping):
        return CheckedNarrowing(dict(narrowing), narrowing_within_frame(frame, narrowing))
    frame_tools = frame.get("tools")
    if not isinstance(frame_tools, Mapping):
        frame_tools = {}

    checked_tools: dict[str, Any] = dict(raw_tools)
    stored_tools: dict[str, Any] = dict(raw_tools)
    for key, raw in raw_tools.items():
        if not isinstance(raw, Mapping):
            continue
        frame_eur = ToolPolicy.from_json(frame_tools.get(key)).approval_eur
        if raw.get("approval_eur") is None and frame_eur is None:
            continue
        if await has_value_spec(db, tenant_id=tenant_id, key=str(key)):
            continue
        if raw.get("approval_eur") is None:
            checked_tools[key] = {**raw, "approval_eur": frame_eur}
        stored_tools[key] = {k: v for k, v in raw.items() if k != "approval_eur"}

    checked = {**narrowing, "tools": checked_tools}
    stored = {**narrowing, "tools": stored_tools}
    return CheckedNarrowing(stored, narrowing_within_frame(frame, checked))


__all__ = ["CheckedNarrowing", "check_agent_narrowing", "has_value_spec"]
