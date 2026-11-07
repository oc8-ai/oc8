"""Loading the acting member's guardrails for a run (D3a).

The acting member is the run's `originating_operator` -- the person who
posted the turn, carried down delegations and wake-ups -- whenever one is
recorded: an admin posting into someone's session acts under the admin's own
guardrails, not the owner's (spec §2.3, plan Review Focus 1).

Only when no operator is recorded does the run fall back to the owner of its
chat session (`chat_session_id`). A messenger turn is started without an
operator, yet it is the bound member talking to their own Copilot, so their
guardrails must still apply.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m


async def acting_member(
    db: AsyncSession, *, tenant_id: uuid.UUID, originating_operator: str | None
) -> m.OrgMember | None:
    """The live member a run acts for, or None (no operator, or since deleted)."""
    if not originating_operator:
        return None
    return (
        await db.execute(
            select(m.OrgMember).where(
                m.OrgMember.tenant_id == tenant_id,
                m.OrgMember.subject == originating_operator,
                m.OrgMember.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def member_term(
    db: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, member: m.OrgMember | None
) -> dict[str, Any] | None:
    """The member's guardrail on this agent -- the third, tighten-only term."""
    if member is None:
        return None
    row = (
        await db.execute(
            select(m.MemberGuardrail).where(
                m.MemberGuardrail.tenant_id == tenant_id,
                m.MemberGuardrail.member_id == member.id,
                m.MemberGuardrail.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    return row.policy if row is not None and row.policy else None


async def session_owner(
    db: AsyncSession, *, tenant_id: uuid.UUID, chat_session_id: object
) -> m.OrgMember | None:
    """The live member who owns a chat session in this tenant, or None."""
    if not chat_session_id:
        return None
    try:
        session_id = uuid.UUID(str(chat_session_id))
    except ValueError:
        return None
    return (
        await db.execute(
            select(m.OrgMember)
            .join(m.ChatSession, m.ChatSession.member_id == m.OrgMember.id)
            .where(
                m.ChatSession.id == session_id,
                m.ChatSession.tenant_id == tenant_id,
                m.OrgMember.tenant_id == tenant_id,
                m.OrgMember.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def member_term_for_run(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    context: Mapping[str, Any] | None,
) -> tuple[m.OrgMember | None, dict[str, Any] | None]:
    """Resolve the acting member from a run context, and their term on the agent.

    The originating operator first; the session owner only when no operator
    is recorded at all (never as a fallback for an operator who is unknown).
    """
    ctx = context or {}
    raw = ctx.get("originating_operator")
    if raw:
        member = await acting_member(db, tenant_id=tenant_id, originating_operator=str(raw))
    else:
        member = await session_owner(
            db, tenant_id=tenant_id, chat_session_id=ctx.get("chat_session_id")
        )
    return member, await member_term(db, tenant_id=tenant_id, agent_id=agent_id, member=member)
