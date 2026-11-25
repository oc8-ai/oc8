"""The one writer of an agent's tool access (`AgentToolAccess`).

Every door that binds an agent to an account goes through `write_tool_access`:
`PUT /agents/{id}/tool-access/{key}` and `POST /mcp/logins` after their own
authorization checks, and the consolidation of old per-department setups as a
system change. It writes the row, stamps the binding fingerprint and its split
(`connections.confirmation.stamp_binding`; in a department with an owner only
where `save_may_stamp` allows it, Ruling FR-C1), switches off
a polled trigger whose login the change takes away, and records the audit
event. Flushes, never commits -- the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.auth import Principal
from oc8.connections.confirmation import (
    binding_held,
    clear_binding_stamp,
    save_may_stamp,
    stamp_binding,
)


def _changed(previous: dict[str, Any], row: m.AgentToolAccess) -> bool:
    """Whether the save changed the row's mode, login or who set it."""
    now = {
        "mode": row.mode,
        "oauth_connection_id": str(row.oauth_connection_id) if row.oauth_connection_id else None,
        "credential_id": str(row.credential_id) if row.credential_id else None,
        "set_by_member_id": str(row.set_by_member_id) if row.set_by_member_id else None,
    }
    return any(previous[key] != value for key, value in now.items())


async def write_tool_access(
    db: AsyncSession,
    *,
    agent: m.Agent,
    tool_key: str,
    mode: str,
    oauth_connection_id: uuid.UUID | None,
    credential_id: uuid.UUID | None,
    set_by_member_id: uuid.UUID | None,
    set_with_manage: bool,
    actor_type: str,
    actor_id: uuid.UUID | None,
    principal: Principal | None = None,
    audit: dict[str, Any] | None = None,
) -> m.AgentToolAccess:
    """Create or replace the agent's access row for `tool_key`. `audit`: extra
    fields of the `agent.tool_access.updated` event (target label, consent)."""
    row = await db.scalar(
        select(m.AgentToolAccess).where(
            m.AgentToolAccess.agent_id == agent.id, m.AgentToolAccess.tool_key == tool_key
        )
    )
    previous = {
        "mode": row.mode if row else None,
        "oauth_connection_id": str(row.oauth_connection_id)
        if row and row.oauth_connection_id
        else None,
        "credential_id": str(row.credential_id) if row and row.credential_id else None,
        "set_by_member_id": str(row.set_by_member_id) if row and row.set_by_member_id else None,
        "set_with_manage": row.set_with_manage if row else None,
    }
    # A system write (consolidation pinning a row) confirms nothing: it keeps
    # the skills that wait for a department owner waiting. A person's save
    # stamps only where Ruling FR-C1 allows it, decided before the save.
    system = actor_type == "system"
    held = await binding_held(db, row, agent)
    may_stamp = system or await save_may_stamp(db, agent, held=held)
    if row is None:
        row = m.AgentToolAccess(
            tenant_id=agent.tenant_id, agent_id=agent.id, tool_key=tool_key, mode=mode
        )
        db.add(row)
    row.mode = mode
    row.oauth_connection_id = oauth_connection_id
    row.credential_id = credential_id
    row.set_by_member_id = set_by_member_id
    row.set_with_manage = set_with_manage
    # Flushed first: the fingerprint reads the agent's access rows, this one
    # included, and the session does not autoflush.
    await db.flush()
    if may_stamp:
        await stamp_binding(db, row, agent, confirmed_by=None if system else actor_id)
        await db.flush()
    elif held is None or _changed(previous, row):
        # Not stamped, and the stamp it carries is not the owner's for this
        # row as it is now (somebody else's, the system's, or of another
        # mode or login): it must not count as theirs. An unchanged row of
        # the owner's keeps its stale stamp, which does not hold either and
        # tells the card what changed.
        clear_binding_stamp(row)
        await db.flush()
    # A trigger polling the login this tool was fixed to stops when the agent's
    # login for it changes, rather than listening on an account the agent's
    # tools no longer reach.
    from oc8.events.sources import disable_off_target_triggers

    await disable_off_target_triggers(db, agent_id=agent.id, tool_key=tool_key)
    await append_event(
        db,
        tenant_id=agent.tenant_id,
        actor_type=actor_type,
        actor_id=actor_id,
        category="authz",
        action="agent.tool_access.updated",
        resource={
            "agent_id": str(agent.id),
            "tool": tool_key,
            "mode": mode,
            "oauth_connection_id": str(oauth_connection_id) if oauth_connection_id else None,
            "credential_id": str(credential_id) if credential_id else None,
            **(audit or {}),
            "previous": previous,
        },
        principal=principal,
    )
    return row
