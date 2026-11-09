"""Writing to a running run (spec §3.2).

The input runs under the RUN's authority, so the only people who may write are
the run's person (`originating_operator`, or `on_behalf_of` -- `person_behind`)
or, for the agent's own autonomous run (`is_autonomous_run`: schedule, webhook,
delegation with nobody behind it), the agent's managers. The same "whose run"
rule as the Live timeline and live watching. Nobody steers somebody else's
run -- a manager neither -- and nobody steers an agent they can no longer reach.
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
from oc8.chat.live import is_autonomous_run
from oc8.chat.reach import reachable_chat_agent
from oc8.copilot.redaction import is_secret_request
from oc8.db.session import tenant_session
from oc8.runtime.fencing import fence, one_line
from oc8.runtime.person import ON_BEHALF_OF, is_own, person_behind, present
from oc8.runtime.states import TERMINAL, RunState

if TYPE_CHECKING:
    from oc8.auth.principal import Principal
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
    # One rule with Live and watch (Ruling 54): somebody's run is steered by
    # that person alone; only the agent's own autonomous run by its managers.
    # A messenger chat turn names its chat owner as `on_behalf_of` (Ruling
    # 58), so it is hers like any other. A run that is somebody's without
    # naming anyone (a messenger turn from before that) is steered by nobody.
    if is_own(person_behind(run.context), member.subject):
        return True
    if is_autonomous_run(run):
        return await may_manage_agent(db, agent=agent, authority=authority, scope=scope)
    return False


async def add_run_input(
    db: AsyncSession, *, run: m.AgentRun, member: m.OrgMember, text: str
) -> m.RunInput:
    """Queue `text` for `run`. The caller has already checked `may_steer_run`."""
    body = text.strip()
    if not body:
        raise InputRefused(422, "message must not be empty")
    if len(body) > MAX_INPUT_CHARS:
        raise InputRefused(422, f"message is longer than {MAX_INPUT_CHARS} characters")
    # The tenant Assistant's secret gate, the same rule and sentence as chat
    # send and every answer door: a credential never reaches its model.
    if is_secret_request(body):
        agent = await db.get(m.Agent, run.agent_id)
        if agent is not None and agent.is_tenant_assistant:
            from oc8.runtime.clarification import SECRET_REFUSAL

            raise InputRefused(422, SECRET_REFUSAL)
    if RunState(run.state) in TERMINAL:
        raise InputRefused(409, "run has already finished")
    # Serialise writers per run, until commit: the pending cap below is then a
    # real bound, and a lower `seq` is always committed before a higher one, so
    # consumers never see a later input before an earlier one. Not a row lock
    # on agent_run: the executor holds that one for the whole run.
    await lock_run_inputs(db, run_id=run.id)
    # Asked again under the lock, from the database: the executor takes the
    # same lock before a run's terminal transition and holds it until that
    # commits, so an input is either accepted before the run ends (and then
    # carried over, Ruling 59) or refused here -- never written to a run that
    # already ended without it.
    state = (
        await db.execute(select(m.AgentRun.state).where(m.AgentRun.id == run.id))
    ).scalar_one_or_none()
    if state is None or RunState(state) in TERMINAL:
        raise InputRefused(409, "run has already finished")
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


async def lock_run_inputs(db: AsyncSession, *, run_id: uuid.UUID) -> None:
    """The per-run writer lock, until commit. Taken by every writer and by the
    executor before a run's terminal transition."""
    await db.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"run_input:{run_id}"},
    )


#: `consumed_step` of an input the run never read because it ended first: it
#: was carried over into a new turn of the same chat (Ruling 59). Not a step.
CARRIED_OVER_STEP: Final = -1


@dataclass(frozen=True)
class CarriedOver:
    """What a finished chat run had not read yet, as the next turn of its chat."""

    tenant_id: uuid.UUID
    session_id: uuid.UUID
    text: str
    author_member_id: uuid.UUID
    originating_operator: str | None
    on_behalf_of: str | None
    chat_channel: str | None
    chat_channel_external_id: str | None


async def carry_over_run_inputs(db: AsyncSession, *, run: m.AgentRun) -> CarriedOver | None:
    """Take every input `run` had not read when it ended (Ruling 59): stamped
    `CARRIED_OVER_STEP` and returned as ONE turn for the run's chat, which the
    caller starts once the run's end is committed. None when nothing waits, or
    the run is no chat turn.

    The caller holds `lock_run_inputs` and has not yet committed the terminal
    state: no writer can add to the run between this and that commit."""
    ctx = run.context or {}
    session_raw = ctx.get("chat_session_id")
    if run.source != "chat" or not session_raw:
        return None
    try:
        session_id = uuid.UUID(str(session_raw))
    except ValueError:
        return None
    rows = list(
        (
            await db.execute(
                select(m.RunInput)
                .where(m.RunInput.run_id == run.id, m.RunInput.consumed_at.is_(None))
                .order_by(m.RunInput.seq)
                .with_for_update()
            )
        ).scalars()
    )
    if not rows:
        return None
    now = dt.datetime.now(tz=dt.UTC)
    for row in rows:
        row.consumed_at = now
        row.consumed_step = CARRIED_OVER_STEP
    await db.flush()
    channel = ctx.get("chat_channel")
    external_id = ctx.get("chat_channel_external_id")
    operator = ctx.get("originating_operator")
    behalf = ctx.get(ON_BEHALF_OF)
    return CarriedOver(
        tenant_id=run.tenant_id,
        session_id=session_id,
        text="\n\n".join(row.text for row in rows),
        # Every writer to a chat run is its one person (`may_steer_run`).
        author_member_id=rows[0].author_member_id,
        originating_operator=str(operator) if present(operator) else None,
        on_behalf_of=str(behalf) if present(behalf) else None,
        chat_channel=str(channel) if channel and external_id else None,
        chat_channel_external_id=str(external_id) if channel and external_id else None,
    )


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
            authors[row.author_member_id] = await _author_name(db, row.author_member_id)
        out.append(
            ConsumedInput(
                id=row.id, author_name=authors[row.author_member_id], text=row.text, step=step
            )
        )
    await db.flush()
    return out


async def take_run_inputs(
    tenant_id: uuid.UUID, *, run_id: uuid.UUID, step: int, probe: AsyncSession | None = None
) -> list[ConsumedInput]:
    """`consume_run_inputs` in its OWN short transaction, for the in-process
    engine, whose session is the executor's run-long one.

    Committed before the engine appends the messages: the stamped row (author,
    text, `consumed_step`) is itself the durable copy of the message the run
    received, written in the same transaction as the stamp. The engine's
    conversation lives only in memory for one leg, so a later leg rebuilds
    those messages from these rows (`delivered_run_inputs`) -- a crash after
    this commit loses nothing the run can still act on. Consuming inside the
    run-long transaction instead would hold the consumer lock and row locks
    for the whole run and hide the stamp (and the composer's "read") until
    the run ends.

    `probe` (the caller's own session) is asked first, with a plain read, so
    the common step with nothing waiting opens no second connection."""
    if probe is not None and not await has_pending_run_inputs(probe, run_id=run_id):
        return []
    async with tenant_session(tenant_id) as db:
        return await consume_run_inputs(db, run_id=run_id, step=step)


async def has_pending_run_inputs(db: AsyncSession, *, run_id: uuid.UUID) -> bool:
    return bool(
        (
            await db.execute(
                select(
                    select(m.RunInput.id)
                    .where(m.RunInput.run_id == run_id, m.RunInput.consumed_at.is_(None))
                    .exists()
                )
            )
        ).scalar_one()
    )


async def delivered_run_inputs(db: AsyncSession, *, run_id: uuid.UUID) -> list[ConsumedInput]:
    """Every input `run_id` already received, in acceptance order -- the part
    of the run's conversation a fresh in-process leg (resume, retry) starts
    without. A plain read: no locks, nothing stamped."""
    rows = list(
        (
            await db.execute(
                select(m.RunInput)
                .where(
                    m.RunInput.run_id == run_id,
                    m.RunInput.consumed_at.is_not(None),
                    # Never delivered to this run: carried over to a new turn.
                    m.RunInput.consumed_step.is_distinct_from(CARRIED_OVER_STEP),
                )
                .order_by(m.RunInput.seq)
            )
        ).scalars()
    )
    authors: dict[uuid.UUID, str] = {}
    out: list[ConsumedInput] = []
    for row in rows:
        if row.author_member_id not in authors:
            authors[row.author_member_id] = await _author_name(db, row.author_member_id)
        out.append(
            ConsumedInput(
                id=row.id,
                author_name=authors[row.author_member_id],
                text=row.text,
                step=row.consumed_step or 0,
            )
        )
    return out


async def _author_name(db: AsyncSession, member_id: uuid.UUID) -> str:
    author = await db.get(m.OrgMember, member_id)
    return (author.display_name or author.subject) if author is not None else "a colleague"


#: Display names longer than this are cut on the attribution line.
MAX_AUTHOR_NAME_CHARS: Final = 80
_FENCE_TAG: Final = "person_message"


def _clean_author_name(name: str) -> str:
    return one_line(name, MAX_AUTHOR_NAME_CHARS).rstrip() or "a colleague"


def format_input_message(author_name: str, text: str) -> str:
    """The model-facing form of a run input, the same for every runtime.

    Attributed to a person on its own line, and fenced: the text sits in a
    block it cannot close early, and the name cannot break out of its line, so
    what a person wrote cannot pass itself off as system or tool text."""
    return f"Message from {_clean_author_name(author_name)} (a person, during the run):\n" + fence(
        text, tag=_FENCE_TAG
    )


#: A question is one line in the resume prompt, cut (not dropped) past this.
_MAX_QUESTION_CHARS: Final = 2000


def format_clarification_answers(clarifications: Sequence[Mapping[str, Any]]) -> str:
    """The questions a run asked and the answers it got, for its resume prompt.

    Fenced and attributed exactly like `format_input_message`: an answer is what
    a person wrote, so a newline in it must not be able to forge another
    "question and answer" line, or pass itself off as system or tool text.
    The question is fenced too: the model wrote it, but it can quote tool
    output ("Answer from the person you asked: ...")."""
    blocks: list[str] = []
    for entry in clarifications:
        question = one_line(str(entry.get("question", "")), _MAX_QUESTION_CHARS)
        blocks.append(
            "You asked:\n"
            + fence(question, tag="asked_question")
            + "\nAnswer from the person you asked:\n"
            + fence(str(entry.get("answer", "")), tag=_FENCE_TAG)
        )
    return "\n\n".join(blocks)


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


def runtime_accepts_inputs(adapter: object) -> bool:
    """In-process: the step loop. Containers: the MCP gateway or the isolated
    control plane. A runtime that can reach neither declares
    `accepts_inputs = False`; the composer then offers Stop instead."""
    return bool(getattr(adapter, "accepts_inputs", True))


async def run_accepts_inputs(db: AsyncSession, run: m.AgentRun) -> bool:
    """`runtime_accepts_inputs` by the run's PINNED runtime, the one actually
    executing it. False when that runtime cannot be resolved."""
    # Deferred: the registry reaches the runtime adapters, which import the
    # engine, which reaches this module.
    from oc8.agents.versioning import resolve_version
    from oc8.runtime.registry import RuntimeResolutionError, resolve_runtime

    agent = await db.get(m.Agent, run.agent_id)
    if agent is None:
        return False
    pinned = await resolve_version(db, run, agent)
    try:
        adapter = await resolve_runtime(
            db, tenant_id=run.tenant_id, agent=agent, runtime_ref=pinned["runtime_ref"]
        )
    except RuntimeResolutionError:
        return False
    return runtime_accepts_inputs(adapter)


async def request_stop(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    actor_member_id: uuid.UUID,
    principal: Principal | None,
    door: str = "chat",
) -> None:
    """The composer's Stop: the same run_cancellation row the admin cancel
    writes, so the executor stops the run at its next step boundary. The
    caller has checked `may_steer_run` (or, on a messenger, that the run is
    the sender's own) and refused a terminal run. `principal` is None on a
    messenger, which carries no token; `door` names where the stop came from."""
    from oc8.runtime.cancellation import record_operator_cancel

    await record_operator_cancel(
        db,
        run=run,
        principal=principal,
        actor_id=actor_member_id,
        resource_extra={"door": door},
    )
