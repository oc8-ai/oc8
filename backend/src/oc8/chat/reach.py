"""May this person chat with -- and so hear about -- this agent?

One predicate, three doors: `POST /chat/sessions` (the HTTP gate, with the
request's resolved authority and scope), and, off-request, the realtime socket's
run watching (`oc8.chat.watch`) and `chat.message_created` delivery
(`oc8.chat.audience`). A member who lost an agent -- a seat revoked, a role
taken away, offboarded -- stops receiving its events at the same moment the
HTTP routes stop admitting them, because both read this.

Reaching the AGENT is enough for the member's own conversation: her own runs
and the runs of her own chats. Any other run of the agent -- its autonomous
runs, a colleague's run under "All runs" -- is a run read like `GET /runs`,
and needs run reach as well (`reaches_agent_runs`, `agents.repo.run_reach`).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.repo import run_reach, run_reachable_agent, visible_agent
from oc8.authz.authority import Authority, authority_for_member, tenant_wide_read
from oc8.authz.permissions import AGENT, COPILOT_USE, RUN, VIEW, perm
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


async def reaches_agent_runs(
    db: AsyncSession,
    *,
    authority: Authority,
    scope: DepartmentScope,
    agent: m.Agent,
    permission: str = perm(RUN, VIEW),
) -> bool:
    """Whether this caller reaches `agent`'s runs with `permission` -- the run
    doors' rule (`agents.repo.run_reach`), asked for a run that is NOT the
    caller's own conversation: an autonomous run, or a colleague's."""
    reach = run_reach(authority=authority, scope=scope, permission=permission)
    reachable = await run_reachable_agent(
        db, reach=reach, tenant_id=agent.tenant_id, agent_id=agent.id
    )
    return reachable is not None


async def member_reaches_agent_runs(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    agent: m.Agent,
    permission: str = perm(RUN, VIEW),
    token_role: str | None = None,
) -> bool:
    """`reaches_agent_runs` for a stored member row, with no request; the same
    `token_role` contract as `member_can_reach_agent`."""
    if member.deleted_at is not None:
        return False
    authority = await authority_for_member(db, member, token_role=token_role)
    scope = await scope_for_member(db, member, token_role=token_role)
    return await reaches_agent_runs(
        db, authority=authority, scope=scope, agent=agent, permission=permission
    )
