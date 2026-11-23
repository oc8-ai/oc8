"""An agent's approval threshold on a connection with no value to compare it
against: the check every writer of an agent's narrowing shares (hire, the
narrowing save, version rollback, and the Copilot's narrowing and guardrail
operations).

`approval_eur` holds a modify at or above an amount, and the amount comes
from the connection's manifest (a `value_spec`). On a connection without one
there is no amount, and the PDP counts a missing amount as ZERO
(`authz.pdp.authorize_tool`/`authorize_tool_call`). So on such a connection:

* `0` is enforced -- every modify asks -- and is kept like any other rule;
* a positive threshold never fires. The frame's own still applies through
  `effective_tool_policies` (the tighter of frame and agent wins), so an
  agent copy equal to the frame's changes nothing and is dropped;
* a new positive value below the frame's (or where the frame sets none)
  would be shown as a guardrail that never holds: refused, by name, never
  dropped silently -- only 0 or the department's value make sense there;
* a looser one is refused by the frame check, like every other rule.

One legacy case is normalised rather than refused: a positive value the
agent already had, sent back unchanged. The page echoes every key on every
save, so when the department changed its threshold (200 -> 300) the stored
200 matched nothing and every save of that agent failed, with the field
hidden (E2E F3 review). That value never fired, so dropping it loosens
nothing; refusing it locked the agent out.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import SubsetViolation, narrowing_within_frame
from oc8.capas.discovery import connection_supports_value_spec, resolve_tool_pack_connection
from oc8.connections.tool_connection import not_retired


@dataclass(frozen=True)
class CheckedNarrowing:
    #: What to store: the narrowing, without thresholds that change nothing.
    narrowing: dict[str, Any]
    #: Where it goes beyond the department frame; empty when it fits.
    violations: list[SubsetViolation]
    #: Keys with a new positive threshold on a connection without a value
    #: spec -- a guardrail that would never hold; empty when there is none.
    unenforceable: list[str]


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


def _threshold(raw: Any) -> float | None:
    value = raw.get("approval_eur") if isinstance(raw, Mapping) else None
    return float(value) if isinstance(value, int | float) else None


async def check_agent_narrowing(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    frame: dict[str, Any],
    narrowing: dict[str, Any],
    stored: dict[str, Any] | None,
) -> CheckedNarrowing:
    """Judge `narrowing` against `frame`, with each threshold on a connection
    without a value_spec judged as the module docstring says. `stored` is the
    agent's narrowing before this write (None for a hire): a value equal to
    its own is one the request did not change.

    The frame check sees a key's threshold as sent, or the frame's own when
    none was sent on such a connection: a looser value is still a violation,
    and leaving it out removes nothing, since the frame's applies anyway.
    Raises ValueError for a malformed policy, as `narrowing_within_frame`
    does."""
    raw_tools = narrowing.get("tools")
    if not isinstance(raw_tools, Mapping):
        return CheckedNarrowing(dict(narrowing), narrowing_within_frame(frame, narrowing), [])
    frame_tools = frame.get("tools")
    if not isinstance(frame_tools, Mapping):
        frame_tools = {}
    stored_tools = stored.get("tools") if isinstance(stored, Mapping) else None
    if not isinstance(stored_tools, Mapping):
        stored_tools = {}

    checked_tools: dict[str, Any] = dict(raw_tools)
    kept_tools: dict[str, Any] = dict(raw_tools)
    unenforceable: list[str] = []
    for key, raw in raw_tools.items():
        if not isinstance(raw, Mapping):
            continue
        sent = _threshold(raw)
        frame_eur = _threshold(frame_tools.get(key))
        if sent is None and frame_eur is None:
            continue
        if await has_value_spec(db, tenant_id=tenant_id, key=str(key)):
            continue
        if sent is None:
            checked_tools[key] = {**raw, "approval_eur": frame_eur}
            continue
        if sent == 0:
            continue  # enforced: a missing amount counts as zero
        if frame_eur is not None and sent > frame_eur:
            continue  # looser: the frame check below refuses it
        unchanged = sent == _threshold(stored_tools.get(key))
        if sent == frame_eur or unchanged:
            kept_tools[key] = {k: v for k, v in raw.items() if k != "approval_eur"}
        else:
            unenforceable.append(str(key))

    checked = {**narrowing, "tools": checked_tools}
    kept = {**narrowing, "tools": kept_tools}
    return CheckedNarrowing(kept, narrowing_within_frame(frame, checked), unenforceable)


__all__ = [
    "CheckedNarrowing",
    "check_agent_narrowing",
    "has_value_spec",
]
