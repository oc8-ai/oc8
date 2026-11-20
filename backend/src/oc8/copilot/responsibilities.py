"""Responsibilities the Copilot keeps for one member (design §7a.3).

Every function is scoped by `member_id` as well as tenant: RLS isolates
tenants, this isolates colleagues. A responsibility another member owns is
"not found", never "forbidden" -- the same non-oracle answer the chat session
routes give."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event

_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"waiting", "paused", "done", "cancelled"}),
    "waiting": frozenset({"active", "paused", "done", "cancelled"}),
    "paused": frozenset({"active", "done", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}
_NOTIFY_RULES = frozenset({"decisions_only", "risks_and_decisions", "every_update"})


class ResponsibilityError(ValueError):
    pass


def can_transition(old: str, new: str) -> bool:
    return new in _TRANSITIONS.get(old, frozenset())


async def get_owned(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, responsibility_id: uuid.UUID
) -> m.Responsibility | None:
    return (
        await db.execute(
            select(m.Responsibility).where(
                m.Responsibility.tenant_id == tenant_id,
                m.Responsibility.member_id == member_id,
                m.Responsibility.id == responsibility_id,
            )
        )
    ).scalar_one_or_none()


async def _require(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, responsibility_id: uuid.UUID
) -> m.Responsibility:
    r = await get_owned(
        db, tenant_id=tenant_id, member_id=member_id, responsibility_id=responsibility_id
    )
    if r is None:
        raise ResponsibilityError("responsibility not found")
    return r


async def open_responsibility(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    chat_session_id: uuid.UUID,
    title: str,
    goal: str,
    notify_rule: str = "risks_and_decisions",
    origin_channel: str | None,
    run_id: uuid.UUID | None,
    actor_agent_id: uuid.UUID,
    member_subject: str,
) -> m.Responsibility:
    if not goal.strip():
        raise ResponsibilityError("a responsibility needs a goal -- what does done look like?")
    if not title.strip():
        raise ResponsibilityError("a responsibility needs a title")
    if notify_rule not in _NOTIFY_RULES:
        raise ResponsibilityError(f"notify_rule must be one of {sorted(_NOTIFY_RULES)}")
    r = m.Responsibility(
        tenant_id=tenant_id,
        member_id=member_id,
        chat_session_id=chat_session_id,
        title=title.strip()[:200],
        goal=goal.strip(),
        notify_rule=notify_rule,
        origin_channel=origin_channel,
        created_by_run_id=run_id,
        last_update_at=dt.datetime.now(tz=dt.UTC),
    )
    db.add(r)
    await db.flush()
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent",
        actor_id=actor_agent_id,
        category="copilot",
        action="copilot.responsibility_opened",
        resource={"responsibility_id": str(r.id), "member_id": str(member_id)},
        originating_operator=member_subject,
    )
    return r


async def update_responsibility(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    responsibility_id: uuid.UUID,
    next_step: str | None = None,
    state: str | None = None,
    report: bool = False,
    run_id: uuid.UUID | None = None,
) -> m.Responsibility:
    r = await _require(
        db, tenant_id=tenant_id, member_id=member_id, responsibility_id=responsibility_id
    )
    if r.state in ("done", "cancelled"):
        raise ResponsibilityError(f"the responsibility is {r.state}")
    if state is not None and state != r.state:
        if state in ("done", "cancelled"):
            raise ResponsibilityError("use close_responsibility to finish a responsibility")
        if not can_transition(r.state, state):
            raise ResponsibilityError(f"cannot move a {r.state} responsibility to {state}")
        r.state = state
    if next_step is not None:
        r.next_step = next_step.strip()[:2000]
    if report and run_id is not None:
        r.last_report_run_id = run_id
    r.last_update_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()
    return r


async def close_responsibility(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    responsibility_id: uuid.UUID,
    state: Literal["done", "cancelled"],
    reason: str,
    actor_agent_id: uuid.UUID | None,
    member_subject: str,
) -> m.Responsibility:
    r = await _require(
        db, tenant_id=tenant_id, member_id=member_id, responsibility_id=responsibility_id
    )
    if not can_transition(r.state, state):
        raise ResponsibilityError(f"cannot move a {r.state} responsibility to {state}")
    now = dt.datetime.now(tz=dt.UTC)
    r.state, r.closed_at, r.close_reason, r.last_update_at = state, now, reason[:500], now
    # Same transaction: a closed responsibility must not wake up again.
    await db.execute(
        update(m.Trigger)
        .where(m.Trigger.tenant_id == tenant_id, m.Trigger.responsibility_id == r.id)
        .values(enabled=False, last_skip_reason=None)
    )
    await db.flush()
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent" if actor_agent_id is not None else "operator",
        actor_id=actor_agent_id,
        category="copilot",
        action=f"copilot.responsibility_{state}",
        resource={"responsibility_id": str(r.id), "member_id": str(member_id)},
        reason=reason[:500],
        originating_operator=member_subject,
    )
    return r


async def list_responsibilities(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    states: Sequence[str] | None = None,
) -> list[m.Responsibility]:
    stmt = select(m.Responsibility).where(
        m.Responsibility.tenant_id == tenant_id, m.Responsibility.member_id == member_id
    )
    if states:
        stmt = stmt.where(m.Responsibility.state.in_(list(states)))
    stmt = stmt.order_by(m.Responsibility.last_update_at.desc().nulls_last())
    return list((await db.execute(stmt)).scalars())
