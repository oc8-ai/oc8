"""May this person chat with -- and so hear about -- this agent?

One predicate, three doors: `POST /chat/sessions` (the HTTP gate, with the
request's resolved authority and scope), and, off-request, the realtime socket's
run watching (`oc8.chat.watch`) and `chat.message_created` delivery
(`oc8.chat.audience`). A member who lost an agent -- a seat revoked, a role
taken away, offboarded -- stops receiving its events at the same moment the
HTTP routes stop admitting them, because both read this.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.repo import visible_agent
from oc8.authz.authority import Authority, authority_for_member, tenant_wide_read
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import DepartmentScope, scope_for_member


async def reachable_chat_agent(
    db: AsyncSession, *, authority: Authority, scope: DepartmentScope, agent_id: uuid.UUID
) -> m.Agent | None:
    """The agent, or `None` for "no row" and "not yours" alike.

    The tenant's Assistant is reachable with `copilot:use` held tenant-wide and
    by nothing else; every other agent through the scoped repository --
    `agent:view` held company-wide, or a seat in its department."""
    if COPILOT_USE in authority.tenant_wide:
        # Loaded unscoped only to learn whether this id IS the Assistant; any
        # other agent is answered by the scoped load below, never by this one.
        row = await visible_agent(
            db, scope=scope, tenant_wide=True, agent_id=agent_id, include_tenant_assistant=True
        )
        if row is not None and row.is_tenant_assistant:
            return row
    return await visible_agent(
        db,
        scope=scope,
        tenant_wide=tenant_wide_read(authority, perm(AGENT, VIEW)),
        agent_id=agent_id,
    )


async def member_can_reach_agent(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    agent_id: uuid.UUID,
    token_role: str | None = None,
) -> bool:
    """`reachable_chat_agent` for a stored member row, with no request.

    `token_role` is the one term storage cannot answer (a member with no
    assigned role resolves to his token's role); pass it only when it is THIS
    member's token. Without it the row terms alone apply -- fail closed."""
    if member.deleted_at is not None:
        return False
    authority = await authority_for_member(db, member, token_role=token_role)
    scope = await scope_for_member(db, member, token_role=token_role)
    agent = await reachable_chat_agent(db, authority=authority, scope=scope, agent_id=agent_id)
    return agent is not None
