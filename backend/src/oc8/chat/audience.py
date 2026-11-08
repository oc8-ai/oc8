"""Who may open a chat session -- the one answer `chat.message_created`
delivery, run watching and (C5) read-only sharing all use."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.reach import member_can_reach_agent


async def session_audience(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    acting_subject: str | None = None,
    acting_role: str | None = None,
) -> list[uuid.UUID]:
    """The session's owner -- as long as the owner can still reach its agent.

    An owner who lost the agent (seat revoked, role taken away, offboarded)
    hears nothing more about it. `acting_role` is the token role of whoever
    caused this (the run's `operator_role`); it is used for the owner only when
    `acting_subject` IS the owner, never to lend one person's role to another."""
    owner = await db.get(m.OrgMember, session.member_id)
    if owner is None or owner.tenant_id != session.tenant_id:
        return []
    token_role = (
        acting_role if acting_subject is not None and acting_subject == owner.subject else None
    )
    if not await member_can_reach_agent(
        db, member=owner, agent_id=session.agent_id, token_role=token_role
    ):
        return []
    return [owner.id]
