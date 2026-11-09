"""May this member receive a run's live events on a member-only socket?"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.audience import session_audience
from oc8.chat.reach import member_can_reach_agent
from oc8.runtime.person import whose_run


async def may_watch_run(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    run_id: uuid.UUID,
    token_role: str | None = None,
) -> bool:
    """A run the member started, or that runs on her behalf or through her
    account (`whose_run`), an autonomous run
    of the agent (it is on their Live timeline), or a chat run of a session
    they own (an edition's readers do NOT stream live) -- never a colleague's run otherwise, not
    even for a manager; in EVERY case only while they can still
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
    whose = await whose_run(db, run)
    if whose.is_own(member.subject):
        return True
    if whose.autonomous:
        # The agent's own run: on the Live timeline of everyone who reaches
        # the agent (spec §3.1), so its live events are theirs too.
        return True
    session_raw = context.get("chat_session_id")
    # Somebody else's run is watched live only from inside its own chat's
    # audience. Never by a manager: the live stream (token deltas, tool
    # arguments, reasoning) carries that person's words; "All runs" shows
    # such a run afterwards, redacted, and nothing more (Ruling 15).
    return bool(session_raw) and await _in_session_audience(
        db, member=member, run=run, session_raw=session_raw, token_role=token_role
    )


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
    # The owner alone: a reader gets no live token, tool or reasoning stream
    # (Ruling 47); they hear `chat.message_created` (ids only) and read the
    # redacted run state.
    audience = await session_audience(
        db,
        session=session,
        acting_subject=member.subject,
        acting_role=token_role,
        readers=False,
    )
    return member.id in audience
