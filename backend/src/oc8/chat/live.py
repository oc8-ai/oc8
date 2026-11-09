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
from typing import Final, Literal

from sqlalchemy import (
    ColumnElement,
    Select,
    String,
    and_,
    cast,
    false,
    func,
    literal,
    not_,
    or_,
    select,
    tuple_,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.runtime.person import PERSON_KEYS, SEVERAL_PEOPLE, is_own, person_behind, present


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


_TRIGGER_KEY_PREFIX: Final = "trigger:"


def trigger_of(
    run: m.AgentRun,
    *,
    delegated_by: str | None = None,
    started_by: str | None = None,
    trigger: m.Trigger | None = None,
) -> dict[str, str | None]:
    """The run's trigger line: `kind` is the run's source, `label` what goes
    beside it -- "Schedule · <cron>", "Event · <source>/<type>", "Webhook",
    "Delegated by <agent>", "From <member>". `trigger` is the Trigger row
    that fired the run, if any (`trigger_names` finds it). A schedule also
    carries `timezone`."""
    label: str | None = None
    if run.source == "delegation":
        label = delegated_by
    elif person_behind(run.context) is not None:
        label = started_by
    elif trigger is not None and run.source == "cron":
        # The zone the schedule is read in: an agent's cron fires in UTC
        # (triggers/service.py) unless the trigger names one.
        return {
            "kind": run.source,
            "label": trigger.cron_expression,
            "timezone": trigger.timezone or "UTC",
        }
    elif trigger is not None and run.source == "event":
        parts = [p for p in (trigger.event_source, trigger.event_type) if p]
        label = "/".join(parts) or None
    return {"kind": run.source, "label": label}


def _id_in(raw: object, *, prefix: str = "") -> uuid.UUID | None:
    if not isinstance(raw, str) or not raw.startswith(prefix):
        return None
    try:
        return uuid.UUID(raw[len(prefix) :])
    except ValueError:
        return None


async def trigger_names(
    db: AsyncSession, runs: Sequence[m.AgentRun], *, tenant_id: uuid.UUID
) -> dict[uuid.UUID, dict[str, str | None]]:
    """`trigger_of` for each run plus `started_by` (the display name of the
    person behind it, `person_behind`), with what it needs read in three batched
    queries, each held to `tenant_id` on top of RLS: the starting member, the
    delegating agent (through the parent task), and the firing Trigger (the
    run's `coalesce_key` is `trigger:<id>`, see triggers/scheduler.py)."""
    subjects = {person for run in runs if (person := person_behind(run.context))}
    parent_tasks = {
        tid
        for run in runs
        if run.source == "delegation"
        and (tid := _id_in((run.context or {}).get("parent_task_id"))) is not None
    }
    trigger_ids = {
        tid for run in runs if (tid := _id_in(run.coalesce_key, prefix=_TRIGGER_KEY_PREFIX))
    }
    members: dict[str, str] = {}
    if subjects:
        member_rows = await db.execute(
            select(m.OrgMember.subject, m.OrgMember.display_name).where(
                m.OrgMember.tenant_id == tenant_id, m.OrgMember.subject.in_(subjects)
            )
        )
        members = {subject: (name or subject) for subject, name in member_rows}
    agents: dict[uuid.UUID, str] = {}
    if parent_tasks:
        agent_rows = await db.execute(
            select(m.Task.id, m.Agent.name)
            .join(m.Agent, m.Agent.id == m.Task.assigned_agent_id)
            .where(
                m.Task.tenant_id == tenant_id,
                m.Agent.tenant_id == tenant_id,
                m.Task.id.in_(parent_tasks),
            )
        )
        agents = {task_id: name for task_id, name in agent_rows}
    triggers: dict[uuid.UUID, m.Trigger] = {}
    if trigger_ids:
        trigger_rows = await db.execute(
            select(m.Trigger).where(m.Trigger.tenant_id == tenant_id, m.Trigger.id.in_(trigger_ids))
        )
        triggers = {row.id: row for row in trigger_rows.scalars()}
    out: dict[uuid.UUID, dict[str, str | None]] = {}
    for run in runs:
        ctx = run.context or {}
        task_id = _id_in(ctx.get("parent_task_id"))
        trigger_id = _id_in(run.coalesce_key, prefix=_TRIGGER_KEY_PREFIX)
        person = person_behind(ctx)
        started_by = members.get(person) if person else None
        out[run.id] = {
            **trigger_of(
                run,
                delegated_by=agents.get(task_id) if task_id else None,
                started_by=started_by,
                trigger=triggers.get(trigger_id) if trigger_id else None,
            ),
            # The PERSON behind the run -- never the delegating agent.
            "started_by": started_by,
        }
    return out


#: Sources a run with nobody behind it comes from. `manual` (POST
#: /agents/{id}/run) and `chat` are always somebody's; see `is_autonomous_run`.
AUTONOMOUS_SOURCES: Final = frozenset(
    {"cron", "event", "webhook", "delegation", "decision", "handoff"}
)


def is_autonomous_run(run: m.AgentRun) -> bool:
    """The agent's own run: an autonomous source, and nobody behind it -- no
    `originating_operator`, no `for_person` (a wake-up or decision follow-up
    continuing a person's work, Ruling 18) and no `chat_session_id` (a
    sub-run delegated from a chat). "Present" is `oc8.runtime.person.present`
    here and in the SQL twin `_autonomous`; both the timeline and live
    watching (`oc8.chat.watch`) use these two and nothing else."""
    ctx = run.context or {}
    return run.source in AUTONOMOUS_SOURCES and not any(
        present(ctx.get(key)) for key in (*PERSON_KEYS, "chat_session_id")
    )


def is_colleague_run(run: m.AgentRun, member: m.OrgMember) -> bool:
    """Somebody else's run: not autonomous and not `member`'s (neither started
    by her nor on her behalf). Shown only under "All runs", and only as step
    labels and result (Ruling 15)."""
    return not is_autonomous_run(run) and not is_own(person_behind(run.context), member.subject)


def _ctx_text(key: str) -> ColumnElement[str]:
    """The key's text, '' for absent or JSON null -- so `== ''` is exactly
    "not `present`" (`->>` renders false, 0 or {} as non-empty text)."""
    return func.coalesce(m.AgentRun.context[key].astext, "")


def _person() -> ColumnElement[str]:
    """`person_behind`, in SQL: '' when nobody."""
    return func.coalesce(*(func.nullif(_ctx_text(key), "") for key in PERSON_KEYS), literal(""))


def _autonomous() -> ColumnElement[bool]:
    """`is_autonomous_run`, in SQL."""
    return and_(
        m.AgentRun.source.in_(AUTONOMOUS_SOURCES),
        *(_ctx_text(key) == "" for key in (*PERSON_KEYS, "chat_session_id")),
    )


def _hers_outside_chat(member: m.OrgMember) -> ColumnElement[bool]:
    """A run started by her or on her behalf, not from a chat (her chat runs
    are her sessions' business, her live-session runs ARE the messages)."""
    # The plain column first: PostgreSQL evaluates an AND's arguments in the
    # order given, and every `context->>` test detoasts the run's (large)
    # context. Chat runs -- most of a chat agent's runs -- stop here.
    return and_(
        m.AgentRun.source != "chat",
        _ctx_text("chat_session_id") == "",
        _person() == member.subject,
        _person() != literal(SEVERAL_PEOPLE),
    )


def _colleagues(member: m.OrgMember) -> ColumnElement[bool]:
    """`is_colleague_run`, in SQL -- and never a run in one of her own
    sessions, whoever is named on it."""
    own_sessions = select(cast(m.ChatSession.id, String)).where(
        m.ChatSession.tenant_id == member.tenant_id, m.ChatSession.member_id == member.id
    )
    return and_(
        not_(_autonomous()),
        or_(_person() != member.subject, _person() == literal(SEVERAL_PEOPLE)),
        not_(_ctx_text("chat_session_id").in_(own_sessions.scalar_subquery())),
    )


def timeline_runs(
    *, agent: m.Agent, member: m.OrgMember, all_runs: bool
) -> Select[tuple[m.AgentRun]]:
    """The agent's runs on `member`'s Live timeline, unordered and unbounded."""
    stmt = select(m.AgentRun).where(
        m.AgentRun.tenant_id == agent.tenant_id,
        m.AgentRun.agent_id == agent.id,
        or_(
            _autonomous(),
            _hers_outside_chat(member),
            _colleagues(member) if all_runs else false(),
        ),
    )
    if not all_runs:
        # Implied by both remaining arms, said once at the top so a chat run
        # is rejected on the column before any JSON is read. The walk itself
        # is `ix_agent_run_agent_created` (agent_id, created_at, id).
        stmt = stmt.where(m.AgentRun.source != "chat")
    return stmt


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
    run_stmt = timeline_runs(agent=agent, member=member, all_runs=all_runs)
    if before is not None:
        if before_id is not None:
            msg_stmt = msg_stmt.where(
                tuple_(m.ChatMessage.created_at, m.ChatMessage.id)
                < tuple_(literal(before), literal(before_id))
            )
            run_stmt = run_stmt.where(
                tuple_(m.AgentRun.created_at, m.AgentRun.id)
                < tuple_(literal(before), literal(before_id))
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
