"""May this member receive a run's live events on a member-only socket?"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.authority import authority_for_member
from oc8.authz.scope import scope_for_member
from oc8.chat.audience import session_audience
from oc8.chat.reach import member_can_reach_agent
from oc8.runtime.run_inputs import may_manage_agent


async def may_watch_run(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    run_id: uuid.UUID,
    token_role: str | None = None,
) -> bool:
    """A run the member started (`originating_operator`), an autonomous run
    of the agent (it is on their Live timeline), a chat run of a session
    whose audience includes them, or -- for the agent's managers ("All
    runs") -- a colleague's chat run; in EVERY case only while they can still
    reach the run's agent. `token_role` is the member's own live
    token role (the socket has it), see `member_can_reach_agent`."""
    run = (await db.execute(select(m.AgentRun).where(m.AgentRun.id == run_id))).scalar_one_or_none()
    if run is None or run.tenant_id != member.tenant_id:
        return False
    if not await member_can_reach_agent(
        db, member=member, agent_id=run.agent_id, token_role=token_role
    ):
        return False
    context = run.context or {}
    if context.get("originating_operator") == member.subject:
        return True
    session_raw = context.get("chat_session_id")
    if run.source != "chat" and not session_raw:
        # An autonomous run: on the Live timeline of everyone who reaches the
        # agent (spec §3.1), so its live events are theirs too.
        return True
    if session_raw and await _in_session_audience(
        db, member=member, run=run, session_raw=session_raw, token_role=token_role
    ):
        return True
    # Somebody else's chat (or a sub-run delegated from it): only the agent's
    # managers, who may switch on "All runs".
    return await _manages_agent(db, member=member, agent_id=run.agent_id, token_role=token_role)


async def _in_session_audience(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    run: m.AgentRun,
    session_raw: object,
    token_role: str | None,
) -> bool:
    try:
        session_id = uuid.UUID(str(session_raw))
    except ValueError:
        return False
    session = await db.get(m.ChatSession, session_id)
    if session is None or session.agent_id != run.agent_id:
        return False
    audience = await session_audience(
        db, session=session, acting_subject=member.subject, acting_role=token_role
    )
    return member.id in audience


async def _manages_agent(
    db: AsyncSession, *, member: m.OrgMember, agent_id: uuid.UUID, token_role: str | None
) -> bool:
    agent = await db.get(m.Agent, agent_id)
    if agent is None:
        return False
    authority = await authority_for_member(db, member, token_role=token_role)
    scope = await scope_for_member(db, member, token_role=token_role)
    return await may_manage_agent(db, agent=agent, authority=authority, scope=scope)
