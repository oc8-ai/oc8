"""Loading the acting member's guardrails for a run (D3a).

The acting member is the run's `originating_operator` -- the person who
posted the turn, carried down delegations and wake-ups. Not the session
owner: an admin posting into someone's session acts under the admin's own
guardrails (spec §2.3, plan Review Focus 1).
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


async def member_term_for_run(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    context: Mapping[str, Any] | None,
) -> tuple[m.OrgMember | None, dict[str, Any] | None]:
    """Resolve the acting member from a run context, and their term on the agent."""
    raw = (context or {}).get("originating_operator")
    member = await acting_member(
        db, tenant_id=tenant_id, originating_operator=str(raw) if raw else None
    )
    return member, await member_term(db, tenant_id=tenant_id, agent_id=agent_id, member=member)
