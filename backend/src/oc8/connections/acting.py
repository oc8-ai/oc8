"""Whose account a run acts through when a tool is "in the user's context"
(connections design).

In this order: the person a delegation or wake-up carried
(`context["on_behalf_of"]`); the chat session's member; the manual run's
starter (`originating_operator`); the creator of the trigger that fired it.
Nobody found is an answer, with a reason -- never a guess.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m

NO_ACTING_PERSON: Final = "no_acting_person"
SCHEDULE_WITHOUT_OWNER: Final = "schedule_without_owner"


@dataclass(frozen=True)
class ActingPerson:
    member_id: uuid.UUID | None
    reason: str | None = None


def _uuid(raw: object) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _person(member_id: uuid.UUID | None) -> ActingPerson:
    return (
        ActingPerson(member_id) if member_id is not None else ActingPerson(None, NO_ACTING_PERSON)
    )


async def _active(db: AsyncSession, member_id: uuid.UUID | None) -> uuid.UUID | None:
    if member_id is None:
        return None
    row = await db.get(m.OrgMember, member_id)
    return row.id if row is not None and row.deleted_at is None else None


async def resolve_acting_person(db: AsyncSession, run: m.AgentRun) -> ActingPerson:
    ctx = run.context or {}
    carried = _uuid(ctx.get("on_behalf_of"))
    if carried is not None:
        return _person(await _active(db, carried))
    session_id = _uuid(ctx.get("chat_session_id"))
    if session_id is not None:
        session = await db.get(m.ChatSession, session_id)
        if session is not None:
            return _person(await _active(db, session.member_id))
    operator = ctx.get("originating_operator")
    if isinstance(operator, str) and operator:
        member_id = await db.scalar(
            select(m.OrgMember.id).where(
                m.OrgMember.tenant_id == run.tenant_id,
                m.OrgMember.subject == operator,
                m.OrgMember.deleted_at.is_(None),
            )
        )
        return _person(member_id)
    trigger_id = _uuid(ctx.get("trigger_id"))
    if trigger_id is not None:
        trigger = await db.get(m.Trigger, trigger_id)
        if trigger is not None:
            if trigger.created_by_member_id is None:
                return ActingPerson(None, SCHEDULE_WITHOUT_OWNER)
            return _person(await _active(db, trigger.created_by_member_id))
    return ActingPerson(None, NO_ACTING_PERSON)
