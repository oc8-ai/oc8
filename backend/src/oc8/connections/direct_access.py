"""Copilot direct access, the runtime side (design §3-§6): which runs may use
it, the grants of a conversation, and the view the PEP decides each call
against. Core-neutral: names no system."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.stages.b_direct_access import NO_DIRECT_ACCESS, DirectAccessView
from oc8.audit import append_event
from oc8.authz.pdp import ToolPolicy
from oc8.connections.acting import ACTING_PERSON_WITHHELD
from oc8.connections.usable import direct_connection, member_departments_for, tool_key_connected
from oc8.copilot.tools import uuid_or_none

#: Successful direct actions after which the Copilot suggests an agent (§11).
SUGGEST_AGENT_AFTER: Final = 2

#: Appended (user role) when the conversation reaches the threshold.
DIRECT_ACTION_NUDGE: Final = (
    "[oc8] You have now acted directly twice in this conversation. Once, in one "
    "sentence, suggest an agent for this kind of work if it is a good fit. "
    "Set one up (enter_agent_mode, propose_agent) only if the person agrees."
)


@dataclass(frozen=True)
class DirectAccessRun:
    member_id: uuid.UUID
    session_id: uuid.UUID


def direct_access_run(run: m.AgentRun | None, agent: m.Agent) -> DirectAccessRun | None:
    """The present member a Copilot run acts for, or None.

    Only a live web turn the conversation's own member wrote: `chat/service`
    stamps `on_behalf_of` on exactly those. Never a follow-up, a messenger
    turn, a delegated or triggered run, or one whose person was withheld
    (§2: only a live conversation with a present member holds a grant)."""
    if run is None or not agent.is_tenant_assistant or run.source != "chat":
        return None
    ctx = run.context if isinstance(run.context, dict) else {}
    if ctx.get(ACTING_PERSON_WITHHELD) or ctx.get("followup") or ctx.get("trigger_id"):
        return None
    if ctx.get("parent_task_id") or ctx.get("door") != "web":
        return None
    member_id = uuid_or_none(ctx.get("on_behalf_of"))
    session_id = uuid_or_none(ctx.get("chat_session_id"))
    if member_id is None or session_id is None:
        return None
    return DirectAccessRun(member_id=member_id, session_id=session_id)


async def active_grants(db: AsyncSession, *, session_id: uuid.UUID) -> list[m.DirectAccessGrant]:
    return list(
        (
            await db.execute(
                select(m.DirectAccessGrant)
                .execution_options(populate_existing=True)
                .where(
                    m.DirectAccessGrant.chat_session_id == session_id,
                    m.DirectAccessGrant.revoked_at.is_(None),
                )
                .order_by(m.DirectAccessGrant.tool_key)
            )
        ).scalars()
    )


async def active_grant(
    db: AsyncSession, *, session_id: uuid.UUID, tool_key: str
) -> m.DirectAccessGrant | None:
    row: m.DirectAccessGrant | None = await db.scalar(
        select(m.DirectAccessGrant)
        .execution_options(populate_existing=True)
        .where(
            m.DirectAccessGrant.chat_session_id == session_id,
            m.DirectAccessGrant.tool_key == tool_key,
            m.DirectAccessGrant.revoked_at.is_(None),
        )
    )
    return row


async def _session_is_members(db: AsyncSession, direct: DirectAccessRun) -> bool:
    session = await db.get(m.ChatSession, direct.session_id, populate_existing=True)
    return session is not None and session.member_id == direct.member_id


async def load_direct_access(
    db: AsyncSession, *, run: m.AgentRun | None, agent: m.Agent
) -> DirectAccessView:
    """Read fresh for every call: a revoked grant, a lost seat or a system
    disconnected since the last call is gone from this view. Never cache it
    for the run."""
    direct = direct_access_run(run, agent)
    if direct is None or not await _session_is_members(db, direct):
        return NO_DIRECT_ACCESS
    granted: dict[str, tuple[tuple[uuid.UUID, ToolPolicy], ...]] = {}
    for row in await active_grants(db, session_id=direct.session_id):
        if row.member_id != direct.member_id:
            continue
        departments = await member_departments_for(
            db, member_id=direct.member_id, tool_key=row.tool_key
        )
        if departments and await tool_key_connected(
            db, tool_key=row.tool_key, department_ids=[d for d, _p in departments]
        ):
            granted[row.tool_key] = tuple(departments)
    return DirectAccessView(member_id=direct.member_id, granted=granted)


async def direct_connections(
    db: AsyncSession, *, run: m.AgentRun | None, agent: m.Agent, right: str = "read"
) -> list[m.McpConnection]:
    """The rows a Copilot run opens: one per granted key, resolved for the
    person (§6) from a department granting `right`, never from the Copilot's
    own department."""
    view = await load_direct_access(db, run=run, agent=agent)
    if not view.granted or run is None:
        return []
    out: list[m.McpConnection] = []
    for key, departments in view.granted.items():
        conn = await direct_connection(
            db,
            tenant_id=run.tenant_id,
            tool_key=key,
            departments=departments,
            right=right,
        )
        if conn is not None:
            out.append(conn)
    return out


async def note_direct_action(
    db: AsyncSession, *, run: m.AgentRun, tool_key: str, tool: str, succeeded: bool
) -> bool:
    """Audit one call executed through a grant ("copilot on behalf of
    <member>"), count a successful one, and say whether this is the one
    after which the Copilot suggests an agent: once per conversation."""
    ctx = run.context or {}
    direct_session = uuid_or_none(ctx.get("chat_session_id"))
    member = uuid_or_none(ctx.get("on_behalf_of"))
    await append_event(
        db,
        tenant_id=run.tenant_id,
        actor_type="agent",
        actor_id=run.agent_id,
        category="copilot",
        action="direct_access.call",
        resource={
            "tool": tool,
            "connection": tool_key,
            "chat_session_id": str(direct_session) if direct_session else None,
            "on_behalf_of": str(member) if member else None,
            "run_id": str(run.id),
            "succeeded": succeeded,
        },
    )
    if not succeeded or direct_session is None:
        return False
    row = await active_grant(db, session_id=direct_session, tool_key=tool_key)
    if row is None:
        return False
    row.actions += 1
    await db.flush()
    rows = (
        (
            await db.execute(
                select(m.DirectAccessGrant).where(
                    m.DirectAccessGrant.chat_session_id == direct_session
                )
            )
        )
        .scalars()
        .all()
    )
    if any(r.agent_suggested_at is not None for r in rows):
        return False
    if sum(r.actions for r in rows) < SUGGEST_AGENT_AFTER:
        return False
    row.agent_suggested_at = dt.datetime.now(dt.UTC)
    await db.flush()
    return True


async def revoke_grant(
    db: AsyncSession, *, grant: m.DirectAccessGrant, member_id: uuid.UUID
) -> None:
    if grant.revoked_at is not None:
        return
    grant.revoked_at = dt.datetime.now(dt.UTC)
    await db.flush()
    await append_event(
        db,
        tenant_id=grant.tenant_id,
        actor_type="operator",
        actor_id=member_id,
        category="copilot",
        action="direct_access.revoked",
        resource={
            "grant_id": str(grant.id),
            "chat_session_id": str(grant.chat_session_id),
            "tool_key": grant.tool_key,
        },
    )
