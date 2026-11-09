"""Read-only chat port for editions (chat search, chat sharing).

An edition reads chats through this and never through internals; every function
decides access here, in core, with `session_audience`. Nothing here writes, and
no run context is exposed. "No access" and "no such session" answer alike."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.audience import session_audience

Access = Literal["owner", "reader"]

#: Upper bound of `search_candidates`, whatever the caller asks for.
MAX_SEARCH_CANDIDATES = 200


@dataclass(frozen=True)
class SessionInfo:
    id: uuid.UUID
    agent_id: uuid.UUID
    title: str
    kind: str
    project_id: uuid.UUID | None
    last_message_at: dt.datetime | None
    access: Access


@dataclass(frozen=True)
class MessageInfo:
    id: uuid.UUID
    role: str
    content: str
    created_at: dt.datetime
    run_id: uuid.UUID | None
    parts: list[dict[str, Any]]


def _info(session: m.ChatSession, access: Access) -> SessionInfo:
    return SessionInfo(
        id=session.id,
        agent_id=session.agent_id,
        title=session.title,
        kind=session.kind,
        project_id=session.project_id,
        last_message_at=session.last_message_at,
        access=access,
    )


async def session_access(
    db: AsyncSession, *, member: m.OrgMember, session: m.ChatSession
) -> Access | None:
    """ "owner" for the member's own session, "reader" when an edition admits
    them (and they still reach the agent), otherwise `None`."""
    if session.tenant_id != member.tenant_id or member.deleted_at is not None:
        return None
    if session.member_id == member.id:
        return "owner"
    return "reader" if member.id in await session_audience(db, session=session) else None


async def list_sessions(db: AsyncSession, *, member: m.OrgMember) -> list[SessionInfo]:
    """The member's own sessions, newest first."""
    rows = await db.execute(
        select(m.ChatSession)
        .where(m.ChatSession.tenant_id == member.tenant_id, m.ChatSession.member_id == member.id)
        .order_by(m.ChatSession.last_message_at.desc().nulls_last(), m.ChatSession.id.desc())
    )
    return [_info(s, "owner") for s in rows.scalars()]


async def get_messages(
    db: AsyncSession, *, member: m.OrgMember, session_id: uuid.UUID
) -> list[MessageInfo] | None:
    """The session's messages as stored, or `None` when it does not exist or
    the member may not open it (the two are indistinguishable)."""
    session = await db.get(m.ChatSession, session_id)
    if session is None or await session_access(db, member=member, session=session) is None:
        return None
    rows = await db.execute(
        select(m.ChatMessage)
        .where(m.ChatMessage.tenant_id == session.tenant_id, m.ChatMessage.session_id == session.id)
        .order_by(m.ChatMessage.id.asc())
    )
    return [
        MessageInfo(
            id=r.id,
            role=r.role,
            content=r.content,
            created_at=r.created_at,
            run_id=r.run_id,
            parts=list(r.parts or []),
        )
        for r in rows.scalars()
    ]


async def search_candidates(
    db: AsyncSession, *, member: m.OrgMember, limit: int = MAX_SEARCH_CANDIDATES
) -> list[SessionInfo]:
    """Sessions the member may open, newest own first, then readable ones: their
    own plus those an edition's readers admit. `limit` is clamped to
    1..MAX_SEARCH_CANDIDATES and also bounds the scan over other members'
    sessions."""
    limit = max(1, min(limit, MAX_SEARCH_CANDIDATES))
    if member.deleted_at is not None:
        return []
    own = (await list_sessions(db, member=member))[:limit]
    if len(own) >= limit:
        return own
    others = await db.execute(
        select(m.ChatSession)
        .where(m.ChatSession.tenant_id == member.tenant_id, m.ChatSession.member_id != member.id)
        .order_by(m.ChatSession.last_message_at.desc().nulls_last(), m.ChatSession.id.desc())
        .limit(limit)
    )
    readable = [
        _info(s, "reader")
        for s in others.scalars()
        if await session_access(db, member=member, session=s) == "reader"
    ]
    return (own + readable)[:limit]
