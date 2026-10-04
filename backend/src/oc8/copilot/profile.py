"""A member's Copilot persona, pause switch and computed status (design §7a.7),
plus the offboarding cleanup (§7a.4). Status is computed, never stored -- a
stored status is a second truth that drifts from the runs it describes."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.runtime.states import RunState

AVATAR_SHAPES = ("round", "square", "drop", "star")
AVATAR_COLORS = ("indigo", "teal", "amber", "rose", "slate", "lime")
Status = Literal["paused", "waiting", "working", "ready"]


class ProfileError(ValueError):
    pass


async def get_or_create_profile(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> m.CopilotProfile:
    stmt = select(m.CopilotProfile).where(
        m.CopilotProfile.tenant_id == tenant_id, m.CopilotProfile.member_id == member_id
    )
    found = (await db.execute(stmt)).scalar_one_or_none()
    if found is not None:
        return found
    try:
        async with db.begin_nested():
            profile = m.CopilotProfile(tenant_id=tenant_id, member_id=member_id)
            db.add(profile)
            await db.flush()
            return profile
    except IntegrityError:
        # Two tabs on first open: the unique constraint picks the winner.
        return (await db.execute(stmt)).scalar_one()


async def update_profile(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    display_name: str | None,
    avatar: dict[str, Any] | None,
) -> m.CopilotProfile:
    profile = await get_or_create_profile(db, tenant_id=tenant_id, member_id=member_id)
    if display_name is not None:
        name = display_name.strip()
        if not 1 <= len(name) <= 40:
            raise ProfileError("display name must be 1-40 characters")
        profile.display_name = name
    if avatar is not None:
        shape, color = avatar.get("shape"), avatar.get("color")
        if shape not in AVATAR_SHAPES or color not in AVATAR_COLORS:
            raise ProfileError("unknown avatar shape or colour")
        profile.avatar = {"shape": shape, "color": color}
    await db.flush()
    return profile


async def _member_session_ids(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> list[str]:
    rows = await db.execute(
        select(m.ChatSession.id).where(
            m.ChatSession.tenant_id == tenant_id, m.ChatSession.member_id == member_id
        )
    )
    return [str(r) for r in rows.scalars()]


async def pause(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> m.CopilotProfile:
    profile = await get_or_create_profile(db, tenant_id=tenant_id, member_id=member_id)
    now = dt.datetime.now(tz=dt.UTC)
    profile.paused_at = now
    sessions = await _member_session_ids(db, tenant_id=tenant_id, member_id=member_id)
    if sessions:
        active = (
            (
                await db.execute(
                    select(m.AgentRun.id).where(
                        m.AgentRun.tenant_id == tenant_id,
                        m.AgentRun.source == "chat",
                        m.AgentRun.state.in_([RunState.QUEUED.value, RunState.RUNNING.value]),
                        m.AgentRun.context["chat_session_id"].astext.in_(sessions),
                    )
                )
            )
            .scalars()
            .all()
        )
        already = set(
            (
                await db.execute(
                    select(m.RunCancellation.run_id).where(
                        m.RunCancellation.tenant_id == tenant_id,
                        m.RunCancellation.run_id.in_(active),
                    )
                )
            ).scalars()
        )
        for run_id in active:
            if run_id in already:
                continue
            # Same row the operator cancel endpoint writes; the executor stops the
            # run at its next step boundary. The kind records who asked.
            db.add(
                m.RunCancellation(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    requested_at=now,
                    cancellation_kind="copilot_paused_by_member",
                )
            )
    await db.flush()
    return profile


async def resume(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> m.CopilotProfile:
    profile = await get_or_create_profile(db, tenant_id=tenant_id, member_id=member_id)
    profile.paused_at = None
    await db.flush()
    return profile


async def computed_status(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, assistant_id: uuid.UUID
) -> tuple[Status, int]:
    profile = await get_or_create_profile(db, tenant_id=tenant_id, member_id=member_id)
    if profile.paused_at is not None:
        return "paused", 0
    sessions = await _member_session_ids(db, tenant_id=tenant_id, member_id=member_id)
    if not sessions:
        return "ready", 0
    in_my_sessions = m.AgentRun.context["chat_session_id"].astext.in_(sessions)
    waiting = await db.scalar(
        select(func.count())
        .select_from(m.AgentRun)
        .where(
            m.AgentRun.tenant_id == tenant_id,
            m.AgentRun.agent_id == assistant_id,
            in_my_sessions,
            m.AgentRun.state.in_(
                [RunState.WAITING_FOR_INPUT.value, RunState.WAITING_FOR_APPROVAL.value]
            ),
        )
    )
    if waiting:
        return "waiting", int(waiting)
    working = await db.scalar(
        select(func.count())
        .select_from(m.AgentRun)
        .where(
            m.AgentRun.tenant_id == tenant_id,
            m.AgentRun.agent_id == assistant_id,
            in_my_sessions,
            m.AgentRun.state.in_([RunState.QUEUED.value, RunState.RUNNING.value]),
        )
    )
    return ("working", int(working)) if working else ("ready", 0)


async def offboard_member(db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID) -> None:
    """Same transaction as the member's soft delete: nothing of theirs wakes again."""
    now = dt.datetime.now(tz=dt.UTC)
    owned = select(m.Responsibility.id).where(
        m.Responsibility.tenant_id == tenant_id, m.Responsibility.member_id == member_id
    )
    await db.execute(
        update(m.Trigger)
        .where(m.Trigger.tenant_id == tenant_id, m.Trigger.responsibility_id.in_(owned))
        .values(enabled=False, last_skip_reason=None)
    )
    await db.execute(
        update(m.Responsibility)
        .where(
            m.Responsibility.tenant_id == tenant_id,
            m.Responsibility.member_id == member_id,
            m.Responsibility.state.notin_(["done", "cancelled"]),
        )
        .values(state="cancelled", closed_at=now, close_reason="member offboarded")
    )
    await db.execute(
        delete(m.CopilotProfile).where(
            m.CopilotProfile.tenant_id == tenant_id, m.CopilotProfile.member_id == member_id
        )
    )
    from oc8.copilot.notes import delete_member_notes

    await delete_member_notes(db, tenant_id=tenant_id, member_id=member_id)
    await db.flush()
