"""Writing to a running run (spec §3.2).

The input runs under the RUN's authority, so the only people who may write are
the person the run already acts for (`originating_operator`) or, for a run with
nobody behind it (schedule, webhook, delegation), the agent's managers. Nobody
steers somebody else's run, and nobody steers an agent they can no longer reach.
The text is only text: guardrails and approvals judge whatever the agent does
next exactly as before.

Inputs are consumed exactly once, in order, at a step boundary
(`consume_run_inputs`). They live in their own table, never in
`agent_run.context`: the executor holds the run row's lock for the whole run.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event
from oc8.authz.authority import Authority
from oc8.authz.permissions import AGENT, MANAGE, perm
from oc8.authz.scope import DepartmentScope
from oc8.chat.reach import reachable_chat_agent
from oc8.runtime.states import TERMINAL, RunState

if TYPE_CHECKING:
    from oc8.realtime.bus import EventBus

#: Mirrors `ck_run_input_text` (migration 0108).
MAX_INPUT_CHARS: Final = 4000
#: Unconsumed inputs one run may hold; bounds what a writer can pile up.
MAX_PENDING_INPUTS: Final = 20


class InputRefused(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class ConsumedInput:
    id: uuid.UUID
    author_name: str
    text: str
    step: int


async def may_manage_agent(
    db: AsyncSession, *, agent: m.Agent, authority: Authority, scope: DepartmentScope
) -> bool:
    """Write rights for the agent: `agent:manage` tenant-wide, or in its department."""
    return perm(AGENT, MANAGE) in authority.tenant_wide or scope.may_manage_agents(
        agent.department_id
    )


async def may_steer_run(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    member: m.OrgMember,
    authority: Authority,
    scope: DepartmentScope,
) -> bool:
    if member.deleted_at is not None or member.tenant_id != run.tenant_id:
        return False
    # Same predicate as every other chat door: a member who lost the agent
    # cannot steer it, even a run he started himself.
    agent = await reachable_chat_agent(db, authority=authority, scope=scope, agent_id=run.agent_id)
    if agent is None:
        return False
    operator = (run.context or {}).get("originating_operator")
    if operator:
        return bool(operator == member.subject)
    return await may_manage_agent(db, agent=agent, authority=authority, scope=scope)


async def add_run_input(
    db: AsyncSession, *, run: m.AgentRun, member: m.OrgMember, text: str
) -> m.RunInput:
    """Queue `text` for `run`. The caller has already checked `may_steer_run`."""
    body = text.strip()
    if not body:
        raise InputRefused(422, "message must not be empty")
    if len(body) > MAX_INPUT_CHARS:
        raise InputRefused(422, f"message is longer than {MAX_INPUT_CHARS} characters")
    if RunState(run.state) in TERMINAL:
        raise InputRefused(409, "run has already finished")
    # Serialise writers per run, until commit: the pending cap below is then a
    # real bound, and a lower `seq` is always committed before a higher one, so
    # consumers never see a later input before an earlier one. Not a row lock
    # on agent_run: the executor holds that one for the whole run.
    await db.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"run_input:{run.id}"},
    )
    pending = (
        await db.execute(
            select(func.count())
            .select_from(m.RunInput)
            .where(m.RunInput.run_id == run.id, m.RunInput.consumed_at.is_(None))
        )
    ).scalar_one()
    if pending >= MAX_PENDING_INPUTS:
        raise InputRefused(429, "too many messages are waiting for this run")
    row = m.RunInput(tenant_id=run.tenant_id, run_id=run.id, author_member_id=member.id, text=body)
    db.add(row)
    await db.flush()
    # The text itself stays out of the audit chain (append-only, unencrypted
    # resource JSON); the row id points at it.
    await append_event(
        db,
        tenant_id=run.tenant_id,
        actor_type="operator",
        actor_id=member.id,
        category="run",
        action="run.input_added",
        resource={
            "run_id": str(run.id),
            "agent_id": str(run.agent_id),
            "input_id": str(row.id),
            "chars": len(body),
        },
        originating_operator=member.subject,
    )
    return row


async def consume_run_inputs(
    db: AsyncSession, *, run_id: uuid.UUID, step: int
) -> list[ConsumedInput]:
    """Take every waiting input for `run_id`, in acceptance order, and stamp it.

    One consumer per run at a time: a rival that finds the per-run consumer
    lock taken gets nothing rather than waiting or taking newer inputs past
    older ones the holder still has. Each input is delivered once, in order.
    The lock is transaction-scoped -- consume in a short transaction."""
    got = (
        await db.execute(
            sql_text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"run_input_consume:{run_id}"},
        )
    ).scalar_one()
    if not got:
        return []
    rows = list(
        (
            await db.execute(
                select(m.RunInput)
                .where(m.RunInput.run_id == run_id, m.RunInput.consumed_at.is_(None))
                .order_by(m.RunInput.seq)
                .with_for_update()
            )
        ).scalars()
    )
    if not rows:
        return []
    now = dt.datetime.now(tz=dt.UTC)
    authors: dict[uuid.UUID, str] = {}
    out: list[ConsumedInput] = []
    for row in rows:
        row.consumed_at = now
        row.consumed_step = step
        if row.author_member_id not in authors:
            author = await db.get(m.OrgMember, row.author_member_id)
            authors[row.author_member_id] = (
                (author.display_name or author.subject) if author is not None else "a colleague"
            )
        out.append(
            ConsumedInput(
                id=row.id, author_name=authors[row.author_member_id], text=row.text, step=step
            )
        )
    await db.flush()
    return out


def format_input_message(author_name: str, text: str) -> str:
    return f"Message from {author_name} during the run: {text}"


def step_hint(context: Mapping[str, Any]) -> int:
    """The step an input consumed now is taken into account in."""
    transcript = context.get("transcript") or []
    assistant_turns = sum(
        1 for entry in transcript if isinstance(entry, dict) and entry.get("role") == "assistant"
    )
    try:
        steps = int(context.get("steps") or 0)
    except (TypeError, ValueError):
        steps = 0
    return max(assistant_turns, steps) + 1


def get_event_bus() -> EventBus:
    # Imported lazily: the bus pulls in notifications, which the runtime must
    # not load at import time.
    from oc8.realtime.bus import get_event_bus as _get_event_bus

    return _get_event_bus()


async def publish_inputs_consumed(
    tenant_id: uuid.UUID, *, run_id: uuid.UUID, consumed: Sequence[ConsumedInput]
) -> None:
    """A `run.*` event: member-only sockets get it only for runs they watch."""
    if not consumed:
        return
    await get_event_bus().publish_event(
        tenant_id,
        "run.input_consumed",
        {
            "run_id": str(run_id),
            "input_ids": [str(c.id) for c in consumed],
            "step": consumed[0].step,
        },
        source=f"oc8/run/{run_id}",
    )
