"""The agent's Live tab (spec §3.1): one continuous conversation per member
and agent, merged at read time with the agent's autonomous runs.

The privacy rule is structural. Another member's chat MESSAGES are never
loaded at all -- only this member's live session is read. A run started from
another member's chat (or delegated from one: such a sub-run carries that
chat's `chat_session_id`) is left out unless a manager asks for "All runs",
and even then it is only a run item: steps and result, never the run's
`task`, which is that member's message (see `LiveRunDTO`). Nothing here is
copied: every item is the stored row it came from."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, String, and_, cast, false, func, not_, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m


@dataclass(frozen=True)
class LiveItem:
    kind: Literal["message", "run"]
    at: dt.datetime
    message: m.ChatMessage | None = None
    run: m.AgentRun | None = None

    @property
    def id(self) -> uuid.UUID:
        row = self.message if self.message is not None else self.run
        assert row is not None
        return row.id


async def get_or_create_live_session(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, agent_id: uuid.UUID
) -> m.ChatSession:
    """The member's live session with the agent, created on first use."""
    stmt = select(m.ChatSession).where(
        m.ChatSession.tenant_id == tenant_id,
        m.ChatSession.member_id == member_id,
        m.ChatSession.agent_id == agent_id,
        m.ChatSession.kind == "live",
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    session = m.ChatSession(
        tenant_id=tenant_id, agent_id=agent_id, member_id=member_id, kind="live", title="Live"
    )
    try:
        async with db.begin_nested():
            db.add(session)
            await db.flush()
    except IntegrityError:
        # A second tab created it first (uq_chat_session_live).
        return (await db.execute(stmt)).scalar_one()
    return session


def trigger_of(
    run: m.AgentRun, *, delegated_by: str | None = None, started_by: str | None = None
) -> dict[str, str | None]:
    """The run's trigger line: `kind` is the run's source, `label` the name
    beside it ("Delegated by <label>", "From <label>"). A schedule's time is
    the item's own `at`, formatted in the viewer's zone by the client."""
    label: str | None = None
    if run.source == "delegation":
        label = delegated_by
    elif (run.context or {}).get("originating_operator"):
        label = started_by
    return {"kind": run.source, "label": label}


async def trigger_names(
    db: AsyncSession, runs: Sequence[m.AgentRun]
) -> dict[uuid.UUID, dict[str, str | None]]:
    """`trigger_of` for each run, with the names it needs read in two queries:
    the delegating agent (through the parent task) and the starting member."""
    subjects = {str(op) for run in runs if (op := (run.context or {}).get("originating_operator"))}
    parent_tasks: set[uuid.UUID] = set()
    for run in runs:
        raw = (run.context or {}).get("parent_task_id")
        if run.source == "delegation" and raw:
            try:
                parent_tasks.add(uuid.UUID(str(raw)))
            except ValueError:
                pass
    members: dict[str, str] = {}
    if subjects:
        rows = await db.execute(
            select(m.OrgMember.subject, m.OrgMember.display_name).where(
                m.OrgMember.subject.in_(subjects)
            )
        )
        members = {subject: (name or subject) for subject, name in rows}
    agents: dict[str, str] = {}
    if parent_tasks:
        rows = await db.execute(
            select(m.Task.id, m.Agent.name)
            .join(m.Agent, m.Agent.id == m.Task.assigned_agent_id)
            .where(m.Task.id.in_(parent_tasks))
        )
        agents = {str(task_id): name for task_id, name in rows}
    out: dict[uuid.UUID, dict[str, str | None]] = {}
    for run in runs:
        ctx = run.context or {}
        out[run.id] = trigger_of(
            run,
            delegated_by=agents.get(str(ctx.get("parent_task_id"))),
            started_by=members.get(str(ctx.get("originating_operator"))),
        )
    return out


def _chat_originated() -> ColumnElement[bool]:
    """Started from a chat, or delegated from one (the sub-run carries the
    chat's session id): somebody's conversation, not an autonomous run."""
    return or_(m.AgentRun.source == "chat", m.AgentRun.context.has_key("chat_session_id"))


def _colleagues_chat(member: m.OrgMember) -> ColumnElement[bool]:
    """A chat-originated run that is neither the member's own nor in one of
    her own sessions -- her live-session runs ARE the timeline's messages,
    and her other chats are not this timeline's business."""
    own_sessions = select(cast(m.ChatSession.id, String)).where(
        m.ChatSession.tenant_id == member.tenant_id, m.ChatSession.member_id == member.id
    )
    return and_(
        func.coalesce(m.AgentRun.context["originating_operator"].astext, "") != member.subject,
        not_(
            func.coalesce(m.AgentRun.context["chat_session_id"].astext, "").in_(
                own_sessions.scalar_subquery()
            )
        ),
    )


async def live_timeline(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    agent: m.Agent,
    session: m.ChatSession,
    before: dt.datetime | None,
    limit: int,
    all_runs: bool,
    before_id: uuid.UUID | None = None,
) -> list[LiveItem]:
    """The `limit` newest items older than the cursor, ascending.

    Ordered by (created_at, id) across both kinds, so paging back with the
    oldest item's (`at`, `id`) as the cursor meets every item exactly once.
    A bare `before` (no id) is a plain time bound. `all_runs` is the caller's
    already-checked manager right."""
    msg_stmt = select(m.ChatMessage).where(
        m.ChatMessage.tenant_id == session.tenant_id, m.ChatMessage.session_id == session.id
    )
    run_stmt = select(m.AgentRun).where(
        m.AgentRun.tenant_id == agent.tenant_id,
        m.AgentRun.agent_id == agent.id,
        or_(
            not_(_chat_originated()),
            _colleagues_chat(member) if all_runs else false(),
        ),
    )
    if before is not None:
        if before_id is not None:
            msg_stmt = msg_stmt.where(
                tuple_(m.ChatMessage.created_at, m.ChatMessage.id) < tuple_(before, before_id)
            )
            run_stmt = run_stmt.where(
                tuple_(m.AgentRun.created_at, m.AgentRun.id) < tuple_(before, before_id)
            )
        else:
            msg_stmt = msg_stmt.where(m.ChatMessage.created_at < before)
            run_stmt = run_stmt.where(m.AgentRun.created_at < before)
    msgs = (
        await db.execute(
            msg_stmt.order_by(m.ChatMessage.created_at.desc(), m.ChatMessage.id.desc()).limit(limit)
        )
    ).scalars()
    runs = (
        await db.execute(
            run_stmt.order_by(m.AgentRun.created_at.desc(), m.AgentRun.id.desc()).limit(limit)
        )
    ).scalars()
    items = [LiveItem("message", msg.created_at, message=msg) for msg in msgs]
    items += [LiveItem("run", run.created_at, run=run) for run in runs]
    items.sort(key=lambda i: (i.at, i.id), reverse=True)
    return sorted(items[:limit], key=lambda i: (i.at, i.id))
