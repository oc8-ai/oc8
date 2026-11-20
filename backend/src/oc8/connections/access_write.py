"""The one writer of an agent's tool access (`AgentToolAccess`).

Every door that binds an agent to an account goes through `write_tool_access`:
`PUT /agents/{id}/tool-access/{key}` and `POST /mcp/logins` after their own
authorization checks, and the consolidation of old per-department setups as a
system change. It writes the row, stamps the binding fingerprint and records
the audit event. Flushes, never commits -- the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.auth import Principal
from oc8.connections.access import binding_fingerprint


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
    row.binding_fingerprint = await binding_fingerprint(
        db, agent, tool_key, version_id=agent.current_version_id
    )
    await db.flush()
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
