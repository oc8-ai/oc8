"""Which agents a person can chat with -- one rule for every door.

The HTTP routes (`api/v1/chat.py`, a member's own guardrails) and the
Copilot's `guardrail_suggest` ask the same question; the request is only where
the first of them finds the authority and the scope.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agents.repo import visible_agent
from oc8.authz.authority import Authority, tenant_wide_read
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import DepartmentScope


async def assistant_visible(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    authority: Authority,
    agent_id: uuid.UUID,
    permission: str = COPILOT_USE,
) -> bool:
    """Whether `agent_id` is the tenant's singleton Assistant AND the caller
    holds `permission` tenant-wide."""
    if permission not in authority.tenant_wide:
        return False
    assistant = await get_or_create_assistant(db, tenant_id=tenant_id)
    return assistant.id == agent_id


async def reachable_chat_agent(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    scope: DepartmentScope,
    authority: Authority,
    agent_id: uuid.UUID,
) -> m.Agent | None:
    """The agent this person may chat with, or None (no such agent, or not
    theirs to chat with -- callers answer both alike)."""
    is_assistant = await assistant_visible(
        db, tenant_id=tenant_id, authority=authority, agent_id=agent_id
    )
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW)) or is_assistant
    return await visible_agent(
        db,
        scope=scope,
        tenant_wide=tenant_wide,
        agent_id=agent_id,
        # `visible_agents`/`visible_agent` hide the Assistant from every screen
        # that browses agents, so this -- the one door that is deliberately
        # ABOUT the Assistant -- has to say so. `assistant_visible` has already
        # established both halves: this caller holds copilot:use, and this id
        # is the tenant's Assistant and nothing else.
        include_tenant_assistant=is_assistant,
    )
