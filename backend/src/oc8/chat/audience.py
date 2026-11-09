"""Who may open a chat session -- the one answer `chat.message_created`
delivery, run watching and the read-only chat port use.

The owner, plus any READERS an edition contributes (read-only sharing lives in
Enterprise). Readers only ever READ: every write route stays owner-only through
`_session_owned_by_caller`, and no authority decision may consult this module
for a reader (the file resolver, steering, stopping ...)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.reach import member_can_reach_agent

logger = logging.getLogger(__name__)

type ExtraReaders = Callable[[AsyncSession, m.ChatSession], Awaitable[Sequence[uuid.UUID]]]

_EXTRA: ContextVar[ExtraReaders | None] = ContextVar("oc8_chat_extra_readers", default=None)


@contextmanager
def _use(fn: ExtraReaders | None) -> Iterator[None]:
    token = _EXTRA.set(fn)
    try:
        yield
    finally:
        _EXTRA.reset(token)


def use_extra_readers(fn: ExtraReaders | None) -> AbstractContextManager[None]:
    """Scope an edition's reader resolver (context-local; default: none)."""
    return _use(fn)


async def extra_readers(db: AsyncSession, *, session: m.ChatSession) -> list[uuid.UUID]:
    """The members an extension admits as readers, filtered here: same tenant,
    existing, not offboarded, not the owner. An extension's ids are never
    trusted as given."""
    fn = _EXTRA.get()
    if fn is None:
        return []
    wanted = [r for r in dict.fromkeys(await fn(db, session)) if r != session.member_id]
    if not wanted:
        return []
    rows = await db.execute(
        select(m.OrgMember.id).where(
            m.OrgMember.id.in_(wanted),
            m.OrgMember.tenant_id == session.tenant_id,
            m.OrgMember.deleted_at.is_(None),
        )
    )
    valid = set(rows.scalars())
    return [r for r in wanted if r in valid]


async def is_reader_only(db: AsyncSession, *, session: m.ChatSession, member_id: uuid.UUID) -> bool:
    """Is `member_id` a reader (not the owner) of `session`? Says nothing about
    reaching the agent -- callers on a request check that themselves."""
    return session.member_id != member_id and member_id in await extra_readers(db, session=session)


async def session_audience(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    acting_subject: str | None = None,
    acting_role: str | None = None,
) -> list[uuid.UUID]:
    """The session's owner and its readers -- each only while they can still
    reach its agent.

    An owner who lost the agent (seat revoked, role taken away, offboarded)
    hears nothing more about it. `acting_role` is the token role of whoever
    caused this (the run's `operator_role`); it is used for the owner only when
    `acting_subject` IS the owner (likewise for a reader), never to lend one
    person's role to another."""
    audience: list[uuid.UUID] = []
    owner = await db.get(m.OrgMember, session.member_id)
    if owner is not None and owner.tenant_id == session.tenant_id:
        if await member_can_reach_agent(
            db,
            member=owner,
            agent_id=session.agent_id,
            token_role=_role_for(owner, acting_subject, acting_role),
        ):
            audience.append(owner.id)
    for reader_id in await extra_readers(db, session=session):
        reader = await db.get(m.OrgMember, reader_id)
        if reader is not None and await member_can_reach_agent(
            db,
            member=reader,
            agent_id=session.agent_id,
            token_role=_role_for(reader, acting_subject, acting_role),
        ):
            audience.append(reader.id)
    return audience


def _role_for(
    member: m.OrgMember, acting_subject: str | None, acting_role: str | None
) -> str | None:
    return acting_role if acting_subject is not None and acting_subject == member.subject else None


async def session_audience_or_nobody(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    acting_subject: str | None = None,
    acting_role: str | None = None,
) -> list[uuid.UUID]:
    """`session_audience` for an event publisher: realtime never breaks a domain
    write, so a failing lookup logs and answers "nobody" (the event is skipped).
    Run inside a SAVEPOINT, so a database error here leaves the caller's
    transaction usable rather than aborted."""
    try:
        async with db.begin_nested():
            return await session_audience(
                db, session=session, acting_subject=acting_subject, acting_role=acting_role
            )
    except Exception:
        logger.warning(
            "could not resolve the audience of chat session %s; event skipped",
            session.id,
            exc_info=True,
        )
        return []
