"""Who may open a `FileAttachment` -- the ONE rule, shared by `GET/DELETE
/files/{id}` (`api/v1/files.py`) and the chat context resolver
(`chat/context/sources.py`), so pinning a file and downloading it can never
answer differently.

Two terms, both required:

* the door -- `agent:view` tenant-wide or in at least one seat, exactly what
  `require_departmental(perm(AGENT, VIEW))` admits on the files routes;
* the row -- a chat file belongs to the OWNER of its session (the only member
  who can upload into it); an instruction file to whoever may see the agent
  it belongs to, through the scoped repository; a run output to whoever may
  see that agent AND reach its runs (`run:view`, `run_reach`) -- a run
  output is a run read.

A missing row, another tenant's row and a refused row all answer False, so a
caller can never tell them apart.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.repo import run_reach, run_reachable_agent, visible_agent
from oc8.authz.authority import Authority, tenant_wide_read
from oc8.authz.permissions import AGENT, RUN, VIEW, perm
from oc8.authz.scope import DepartmentScope

_AGENT_VIEW = perm(AGENT, VIEW)


def passes_files_door(authority: Authority, scope: DepartmentScope) -> bool:
    """`require_departmental(perm(AGENT, VIEW))`'s admission test."""
    return _AGENT_VIEW in authority.tenant_wide or scope.holds_anywhere(_AGENT_VIEW)


async def _session(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> m.ChatSession | None:
    return (
        await db.execute(
            select(m.ChatSession).where(
                m.ChatSession.tenant_id == tenant_id, m.ChatSession.id == session_id
            )
        )
    ).scalar_one_or_none()


async def chat_session_of(
    db: AsyncSession, *, tenant_id: uuid.UUID, owner_id: uuid.UUID
) -> m.ChatSession | None:
    """A chat file's session. `owner_id` changes meaning partway through a
    turn: it is the `ChatSession.id` from upload until the message carrying the
    file is sent, and the `ChatMessage.id` from `send_message`'s re-point on.
    The session lookup goes first because that is the common case (most files
    are read back moments after upload)."""
    session = await _session(db, tenant_id, owner_id)
    if session is not None:
        return session
    message = await db.get(m.ChatMessage, owner_id)
    if message is None or message.tenant_id != tenant_id:
        return None
    return await _session(db, tenant_id, message.session_id)


async def member_may_open_attachment(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    authority: Authority,
    scope: DepartmentScope,
    row: m.FileAttachment,
) -> bool:
    tenant_id = member.tenant_id
    if row.tenant_id != tenant_id or not passes_files_door(authority, scope):
        return False
    if row.owner_type in ("agent_instructions", "agent_run"):
        if row.owner_type == "agent_run":
            run = await db.get(m.AgentRun, row.owner_id)
            if run is None or run.tenant_id != tenant_id:
                return False
            agent_id = run.agent_id
        else:
            agent_id = row.owner_id
        agent = await visible_agent(
            db,
            scope=scope,
            tenant_wide=tenant_wide_read(authority, _AGENT_VIEW),
            agent_id=agent_id,
        )
        if agent is None:
            return False
        if row.owner_type == "agent_run":
            reach = run_reach(authority=authority, scope=scope, permission=perm(RUN, VIEW))
            reachable = await run_reachable_agent(
                db, reach=reach, tenant_id=tenant_id, agent_id=agent_id
            )
            return reachable is not None
        return True
    session = await chat_session_of(db, tenant_id=tenant_id, owner_id=row.owner_id)
    return session is not None and session.member_id == member.id
