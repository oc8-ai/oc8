"""Drive a queued AgentRun through the resolved runtime and persist the
outcome. Runs outside any HTTP request, so it binds the tenant explicitly."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.observability import get_tracer, record_run_outcome
from oc8.realtime.emit import publish_agent_status, record_activity
from oc8.runtime.adapter import RuntimeAdapter
from oc8.runtime.intake import publish_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.reconcile import ABANDONED_AFTER, heartbeat
from oc8.runtime.registry import resolve_runtime
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import merge_context
from oc8.runtime.states import RunState
from oc8.runtime.workspace import sync_run_output

logger = logging.getLogger(__name__)

_STATUS_TO_STATE: dict[str, RunState] = {
    "done": RunState.DONE,
    "failed": RunState.FAILED,
    "interrupted": RunState.INTERRUPTED,
    "waiting_for_approval": RunState.WAITING_FOR_APPROVAL,
    "budget_exceeded": RunState.WAITING_FOR_APPROVAL,
}


async def _cancellation_kind(db: AsyncSession, run_id: uuid.UUID) -> str | None:
    """The cancellation_kind of the run's run_cancellation row, or None if the
    run has not been cancelled. A light SELECT on the run_cancellation table --
    a row the executor's long transaction does NOT lock -- so it is safe to call
    inside that transaction and observes the cancel endpoint's committed insert
    under READ COMMITTED."""
    return (
        await db.execute(
            select(m.RunCancellation.cancellation_kind).where(m.RunCancellation.run_id == run_id)
        )
    ).scalar_one_or_none()


async def _tell_channel(*, tenant_id: uuid.UUID, channel: str, external_id: str, text: str) -> None:
    """Best-effort: a chat channel send failure must never fail the run whose
    answer it is carrying. `say` is not part of the `ApprovalChannel`
    Protocol (it's a duck-typed extra a channel plugin may offer) -- reuses
    `dispatch.tell_sender_gated` rather than calling `impl.say` directly,
    same as every other best-effort reply in `oc8.channels.dispatch` itself.
    The `_gated` variant is deliberate: every text this function ever carries
    -- the run's real answer, a park notice, a failure notice -- is an
    Assistant-produced reply and gets the same classification check an
    approval's own detail already gets against this channel's
    `max_classification`. `channel` is read off the run's own context
    (`chat_channel`) rather than hardcoded, so Telegram, Teams, and any
    later channel all reach this same function."""
    from oc8.channels.dispatch import tell_sender_gated
    from oc8.channels.registry import channels_for_tenant

    try:
        async with tenant_session(tenant_id) as db:
            channels = await channels_for_tenant(db, tenant_id=tenant_id)
    except Exception:
        logger.warning("could not load %s channel to reply", channel, exc_info=True)
        return
    impl = channels.get(channel)
    if impl is None:
        return
    await tell_sender_gated(impl, external_id, text)


#: What a Telegram sender is told when their chat run PARKS rather than
#: finishes. Without these the ack ("Bin dran, melde mich gleich.") was the last
#: thing they ever heard: the reply hook only fired on DONE/FAILED, so a run
#: that stopped to ask a question or to wait for an approval simply went quiet,
#: and the person had no way to know anything was expected of them.
_CHAT_NEEDS_INPUT = "I need more information to continue -- please answer in oc8."
_CHAT_WAITING_FOR_APPROVAL = "This needs an approval before I can continue -- see oc8."
#: The transcript message a crashed chat run leaves behind. Deliberately NOT
#: `repr(exc)`: the exception text is already on the run (`context["error"]`)
#: where an operator can read it, and a chat transcript is the one place it
#: would be shown to whoever happens to be in the conversation.
_CHAT_RUN_FAILED = "That didn't work out -- see the run in oc8 for what happened."


def _chat_channel_sender_of(run: m.AgentRun) -> tuple[str, str] | None:
    """The (channel id, external id) a `source="chat"` run should reply on,
    if it was started from a channel at all. None for a web chat turn and
    for every other source -- both of which have their own surface to read
    the outcome on. Returns the pair, not just the external id: choosing
    WHICH channel to reply on is the entire point of this function once more
    than one channel exists."""
    if run.source != "chat":
        return None
    context = run.context or {}
    channel = context.get("chat_channel")
    external_id = context.get("chat_channel_external_id")
    if not channel or not external_id:
        return None
    return str(channel), str(external_id)


async def requeue_if_already_decided(db: AsyncSession, *, run: m.AgentRun) -> bool:
    """Re-queue a just-parked run whose decision has already been made.

    `resolve_tool_approval` only re-queues a run that is ALREADY parked, and the
    park is not instantaneous: the gateway writes its marker, the adapter notices
    on its next poll, the container is torn down, and the transition happens after
    that. An operator deciding inside that window recorded the decision against a
    RUNNING run, nothing re-queued it, and the run then waited forever for a
    decision that had already been made -- while the held action may well have
    executed already.

    `resolved_tool_approvals` accumulates across every approval cycle a run goes
    through, so a run parking for a SECOND, still-undecided call already carries
    the first call's entry. Re-queuing on mere presence of that list would wake
    the run before anyone decided the new call -- wrongly winning the very race
    this function exists to close. So match the signature of the call THIS park
    is actually holding (the ApprovalRequest tied to the run's task, tool_send,
    most recently created) against the recorded entries, rather than asking only
    "is anything recorded at all".

    This covers only the HALF of the parking race where the decision was already
    committed by the time this transition runs (this function's own SELECT then
    sees it). The other half -- a decision whose write blocks on THIS
    transition's row lock and lands the instant it releases -- cannot be caught
    from here, because by the time our caller reaches this point the transition
    is already committed and there is nothing left in this transaction to race
    against. That half is closed on the other side, by the re-read in
    `resolve_tool_approval` (approval_resume.py) after ITS flush unblocks.
    Together the two leave no window: every decision is either visible here
    already, or the operator's own re-read sees the park it was blocked behind.
    """
    entries = (run.context or {}).get("resolved_tool_approvals") or []
    if not entries or run.task_id is None:
        return False

    from oc8.runtime.approval_resume import call_signature

    current = (
        await db.execute(
            select(m.ApprovalRequest)
            .where(
                m.ApprovalRequest.task_id == run.task_id,
                m.ApprovalRequest.action_type == "tool_send",
            )
            .order_by(m.ApprovalRequest.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if current is None:
        return False
    sig = call_signature(
        str(current.payload.get("tool", "")), current.payload.get("arguments") or {}
    )
    if not any(e.get("sig") == sig for e in entries):
        return False

    await RunRepository(db).transition(run, RunState.QUEUED)
    return True


async def agent_has_own_login_binding(db: AsyncSession, agent: m.Agent) -> bool:
    """Whether this agent's OWN narrowing already claims a tool key that an
    inherited connection must never be allowed to shadow.

    True when any of the agent's ENABLED tool keys either (a) already carries a
    pinned connection_id, or (b) names a Credential-backed login even if
    unpinned -- in both cases the agent must resolve that key for itself (find
    its own pin, or fail loudly for the want of one) rather than silently
    inheriting a department connection propagated from a delegating lead or a
    finishing sub-agent.

    Why (b) matters as much as (a): `_resolve_mcp_connection` reads
    `run_context["mcp_connection_id"]` FIRST and returns on it, so an inherited
    id short-circuits the whole per-agent resolution -- including the loud
    "needs a login but none is assigned" error. A department connection is
    harmless to inherit only for an agent that claims nothing of its own.

    Used by control_tools._delegate and executor._maybe_wake_parent to decide
    whether to propagate an inherited mcp_conn at all. Deliberately public: the
    other call site lives in another module.
    """
    raw_tools = (agent.narrowing or {}).get("tools") or {}
    tools: dict[str, Any] = raw_tools if isinstance(raw_tools, dict) else {}
    enabled = [
        key for key, policy in tools.items() if isinstance(policy, dict) and policy.get("enabled")
    ]
    if not enabled:
        return False
    if any(tools[key].get("connection_id") for key in enabled):
        return True
    # Same query `_resolve_mcp_connection` runs for its own needs_login set: any
    # enabled key that names a login is a key this agent has to resolve itself.
    needs_login = (
        (
            await db.execute(
                select(m.McpConnection.name).where(
                    m.McpConnection.tenant_id == agent.tenant_id,
                    m.McpConnection.name.in_(enabled),
                    m.McpConnection.credential_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return bool(needs_login)


async def _maybe_wake_parent(
    db: AsyncSession,
    *,
    repo: RunRepository,
    tenant_id: uuid.UUID,
    parent_task_id: uuid.UUID | None,
    delegation_depth: int,
    finished_agent_id: uuid.UUID,
    sub_task_label: str,
    output: str,
    succeeded: bool,
    mcp_conn: m.McpConnection | None,
    chat_session_id: str | None = None,
    chat_channel: str | None = None,
    chat_channel_external_id: str | None = None,
) -> uuid.UUID | None:
    """Create a follow-up run for the team lead that delegated this sub-run, so
    it can react to the outcome (§7). Returns the new run's id for the caller to
    publish after committing, or None when nothing should be woken. Does NOT
    commit or publish -- it runs inside execute_run's still-bound transaction.

    Driven entirely by the sub-run's own context (parent_task_id, delegation_depth)
    and its agent id, so it works identically whether the sub-run finished
    normally or died with an exception (run_agent signals real failure by
    raising, never via status="failed"). A failed sub-task wakes the lead with
    the failure text, so the lead's model decides whether to re-delegate -- that
    is spec §7 reassignment, with no dedicated mechanism.
    """
    if parent_task_id is None:
        return None
    parent = await db.get(m.Task, parent_task_id)
    if parent is None or parent.assigned_agent_id is None:
        return None
    # A team lead's own wake-up task is parented to its earlier task, and both
    # are assigned to the lead. Without this guard that would wake it forever,
    # and delegation_depth -- which a wake-up carries unchanged, being a
    # continuation rather than a new hop -- would never rise to stop it.
    # Self-delegation, the other way to produce this shape, is denied in the
    # engine's _authorize. finished_agent_id is the sub-run's own agent, i.e.
    # the sub-task's assigned agent.
    if parent.assigned_agent_id == finished_agent_id:
        return None
    # Past that guard the parent is a real, different agent, so its row is worth
    # loading: the propagation decision below depends on what IT claims.
    parent_agent = await db.get(m.Agent, parent.assigned_agent_id)

    outcome = "completed" if succeeded else "failed"
    context: dict[str, Any] = {
        "task": f'Sub-task "{sub_task_label[:80]}" {outcome}.\n\nResult: {output}',
        "parent_task_id": str(parent.id),
        "delegation_depth": delegation_depth,
    }
    # Two reasons the sub-run's connection is withheld from the lead's wake-up,
    # and both end the same way: the lead resolves for itself.
    #
    # A Credential-backed LOGIN is never propagated. It belongs to the sub-agent
    # that just finished, and putting its id in the lead's context would win over
    # the lead's own pin (an explicit context id outranks a narrowing pin) and
    # suppress the missing-pin error too -- the lead would act in the external
    # system as the sub-agent.
    #
    # A department connection normally IS propagated: it is shared by
    # construction, so it is the same connection the lead would have found for
    # itself. The exception is a lead whose own narrowing already claims one of
    # its tool keys -- pinned, or naming a login it never pinned. There the
    # inherited id would be read first and returned, so a lead missing its pin
    # would quietly borrow the department's connection instead of failing loudly.
    # Withhold it and let the lead's own resolution have the last word.
    if (
        mcp_conn is not None
        and mcp_conn.credential_id is None
        and parent_agent is not None
        and not await agent_has_own_login_binding(db, parent_agent)
    ):
        context["mcp_connection_id"] = str(mcp_conn.id)
    # A wake-up whose delegation chain traces back to a chat turn is treated
    # as a chat turn itself: source="chat" is what the terminal-state path
    # below reads (record_assistant_reply, and _tell_channel) to decide
    # whether an outcome is user-facing at all. Without this the lead's real
    # conclusion after a delegated sub-task -- the only part of "ask the
    # Assistant something, it delegates, you get the answer" a human
    # actually cares about -- was created with source="delegation" and never
    # reached either. chat_session_id/chat_channel/chat_channel_external_id
    # ride along unchanged from the finishing sub-run's own context (itself
    # carried forward from delegate_task, see control_tools._delegate)
    # rather than being re-derived here, so a delegation chain several hops
    # deep keeps pointing at the SAME original conversation, on the SAME
    # channel, at every hop.
    wake_source = "chat" if chat_session_id else "delegation"
    if chat_session_id:
        context["chat_session_id"] = chat_session_id
        if chat_channel and chat_channel_external_id:
            context["chat_channel"] = chat_channel
            context["chat_channel_external_id"] = chat_channel_external_id
    wake = await repo.create(
        tenant_id=tenant_id,
        agent_id=parent.assigned_agent_id,
        context=context,
        source=wake_source,
    )
    return wake.id


class RunDeferred(Exception):
    """Not now: this agent already has a run in flight.

    Not an error, and deliberately not a failure state on the run -- the work is
    real and still wanted. The worker leaves the stream entry unacked, so the
    same message comes back after the idle window and is tried again.
    """


class _ConnectionChoice(NamedTuple):
    """The outcome of resolving a run's McpConnection.

    `pinned` says the choice came from an EXPLICIT selection -- the agent's
    own narrowing, or the department's own default-login setting for that
    tool -- rather than from an inherited run context or the untargeted
    legacy department fallback. The caller needs that distinction: only an
    explicit choice is stamped onto the run context (see `execute_run`),
    because only an explicit choice is an identity the container runtime
    would otherwise resolve differently (its own, independent department_id
    lookup would not know a department chose a DIFFERENT connection than
    the one that lookup would guess).

    `connections` is every pin that resolved (one or several). `.connection`
    is the first of those (alphabetically by tool key) so callers that still
    read a single row -- control tools, the in-process `mcp_conn` argument --
    keep a stable primary. A second pin used to be dropped here; it is now
    carried through and stamped as `mcp_connection_ids`.
    """

    connection: m.McpConnection | None
    error: str | None
    pinned: bool = False
    connections: tuple[m.McpConnection, ...] = ()


async def _resolve_mcp_connection(
    db: AsyncSession, *, agent: m.Agent, run_context: dict[str, Any]
) -> _ConnectionChoice:
    """Which McpConnection this agent's run uses, and why it can't have one.

    Exactly one of `connection`/`error` is ever set, except for the legitimate
    both-None: an agent that reaches no system at all, which has always been
    allowed and still runs. `connections` may hold several pins when the agent
    was assigned more than one login; `.connection` is still the first of
    those. A non-None `error` is a HARD failure -- the caller fails the run
    with it rather than starting a runtime that would act through the wrong
    login.

    Priority:

    1. An explicit `run.context["mcp_connection_id"]` -- a delegation, or an
       operator's "run against THIS connection". Today's escape hatch, first as
       it has always been, and its unresolvable case keeps its own message.
    2. The agent's own narrowing-pinned login (agent tool login selection
       design), OR -- if the agent has no pin of its own -- the department's
       own `default_connection_id` for that tool (`frame["tools"][key]`,
       `set_department_tools`). An operator assigned this agent (or its
       department) this login for this tool; a pin still always wins over the
       department default when both are set, which is the whole point of
       letting an agent override it.
    3. ANY tool the agent has ENABLED which needs a login, with nothing pinned
       to it AND no department default set for it: an error, never a guess --
       checked across every enabled key BEFORE step 2 can succeed, so a
       correct pin (or default) on one tool cannot mask a missing one on
       another. `PUT /agents/{id}/narrowing` refuses to write this state, so
       reaching it means something bypassed that check (a tool pack that
       gained a login requirement after the agent was configured, a
       department cascade, a direct DB edit). The runtime defends the invariant
       itself rather than trusting the writer.
    4. Otherwise the legacy, department-scoped lookup -- unchanged for every
       connection that predates this design and every department that never
       set an explicit default, and narrowed only by `credential_id IS NULL`
       so it can never hand over a login nobody pinned or defaulted to.
       A login is tenant-global today (`POST /mcp/logins` sets no
       department_id), so that filter changes no live row; it is there so the
       guarantee survives a login that ever does get a department.
    """
    mcp_id = run_context.get("mcp_connection_id")
    if mcp_id:
        conn = await db.get(m.McpConnection, uuid.UUID(str(mcp_id)))
        if conn is None:
            return _ConnectionChoice(None, f"unknown MCP connection: {mcp_id}")
        return _ConnectionChoice(conn, None)

    raw_tools = (agent.narrowing or {}).get("tools") or {}
    tools: dict[str, Any] = raw_tools if isinstance(raw_tools, dict) else {}
    # Sorted so two runs of the same agent always decide identically. Keys are
    # connection NAMES (the frame keys tools by name, not id) -- the same shape
    # `PUT /agents/{id}/narrowing` validates against.
    enabled = sorted(
        key for key, policy in tools.items() if isinstance(policy, dict) and policy.get("enabled")
    )

    #: Which of this agent's enabled tool keys name a Credential-backed login.
    #: One query for the whole set, because the check below has to consider
    #: EVERY enabled key, not just the first one that happens to carry a pin.
    needs_login: set[str] = set()
    if enabled:
        needs_login = set(
            (
                await db.execute(
                    select(m.McpConnection.name).where(
                        m.McpConnection.tenant_id == agent.tenant_id,
                        m.McpConnection.name.in_(enabled),
                        m.McpConnection.credential_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    # The department's own per-tool defaults (department-scoped-agent-authority
    # design's `frame["tools"]`, the same JSONB `set_department_tools` writes).
    # Fetched once, ahead of both loops below: an operator can set
    # `default_connection_id` on a tool to name a specific McpConnection this
    # department's agents use unless they have their own narrowing pin --
    # deliberately checked in BOTH loops below (the missing-pin error and the
    # pin-resolution loop), the same way an agent's own pin is, so a
    # department default satisfies a `needs_login` tool exactly as a pin
    # would, and wins over the untargeted legacy department lookup exactly
    # like a pin does.
    dept_tools: dict[str, Any] = {}
    if enabled and agent.department_id is not None:
        dept = await db.get(m.Department, agent.department_id)
        if dept is not None:
            raw_dept_tools = (dept.frame or {}).get("tools")
            if isinstance(raw_dept_tools, dict):
                dept_tools = raw_dept_tools

    def _dept_default(key: str) -> str | None:
        entry = dept_tools.get(key)
        if isinstance(entry, dict):
            value = entry.get("default_connection_id")
            if isinstance(value, str) and value:
                return value
        return None

    # An unpinned login-requiring tool fails the run even when some OTHER tool
    # is pinned correctly -- UNLESS the department itself named a default
    # connection for this key, which stands in for a pin exactly as the loop
    # below treats it. Returning the other tool's connection here would let
    # a half-configured agent run as though it were fully configured, which is
    # exactly the silent fallback this design forbids -- and it is the promise
    # `set_department_tools` already makes on this function's behalf ("an agent
    # cascaded to enabled without a connection_id is caught at runtime
    # resolution"). That promise has to hold for every enabled key.
    for key in enabled:
        if key in needs_login and not tools[key].get("connection_id") and not _dept_default(key):
            return _ConnectionChoice(
                None,
                f"tool {key!r} needs a login but none is assigned to this agent: "
                "set connection_id on the agent's narrowing for this tool",
            )

    pinned_conns: list[m.McpConnection] = []
    for key in enabled:
        # The agent's own pin always wins over the department's default --
        # that is the whole point of letting an agent override it.
        pinned_id = tools[key].get("connection_id") or _dept_default(key)
        if not pinned_id:
            continue
        try:
            pinned_uuid = uuid.UUID(str(pinned_id))
        except ValueError:
            # narrowing is operator-supplied JSONB; a malformed pin must fail
            # this run loudly, not raise out of execute_run's pre-try section
            # and take the worker's message handling with it.
            return _ConnectionChoice(
                None, f"tool {key!r} has an unreadable pinned login: {pinned_id!r}"
            )
        conn = await db.get(m.McpConnection, pinned_uuid)
        if conn is None:
            return _ConnectionChoice(
                None, f"tool {key!r} is pinned to a login that no longer exists: {pinned_id}"
            )
        # A pin names an id, but a tool key names a CONNECTION NAME -- nothing
        # upstream checks the two agree, so an operator who pastes the wrong id
        # would otherwise have this agent act in a system it was never assigned.
        # Same by-name convention Task 3/4 established; treated as a missing pin.
        if conn.name != key:
            return _ConnectionChoice(
                None,
                f"tool {key!r} is pinned to connection {conn.name!r}, which is a "
                "different tool: re-assign this agent's login for this tool",
            )
        if key in needs_login and conn.credential_id is None:
            # This key HAS logins; a pin here must select one of them. A shared,
            # department-scoped connection is not a login, and substituting it
            # would be the identity swap the pin exists to prevent.
            return _ConnectionChoice(
                None,
                f"tool {key!r} needs a login, but its pinned connection {pinned_id} "
                "is not one: re-assign this agent's login for this tool",
            )
        pinned_conns.append(conn)
    if pinned_conns:
        return _ConnectionChoice(
            pinned_conns[0], None, pinned=True, connections=tuple(pinned_conns)
        )

    # Department-scoped integrations are the normal plugin path: an operator
    # configures a tool pack once, then every agent in that department can use
    # its tested connection without the caller knowing an internal MCP
    # connection id. An explicit run context above still wins for
    # advanced/manual use cases.
    legacy = (
        await db.execute(
            select(m.McpConnection)
            .where(
                m.McpConnection.department_id == agent.department_id,
                m.McpConnection.connected.is_(True),
                m.McpConnection.credential_id.is_(None),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return _ConnectionChoice(legacy, None)


async def execute_run(message: RunMessage, *, runtime: RuntimeAdapter | None = None) -> None:
    run_id = uuid.UUID(message["run_id"])
    tenant_id = uuid.UUID(message["tenant_id"])
    pending_runs: list[uuid.UUID] = []
    #: (tenant_id, external_id, text) for every Telegram reply this run's
    #: terminal state earned. Collected here and sent only AFTER the
    #: `tenant_session` block below commits -- same reason `pending_runs`
    #: is published only after commit (see the comment at that loop): a
    #: reply sent before the transition/record_assistant_reply write is
    #: durable would hand the user an answer the system never recorded, and
    #: a redelivery would then send it a second time.
    channel_replies: list[tuple[uuid.UUID, str, str, str]] = []

    with get_tracer().start_as_current_span("run.execute") as span:
        span.set_attribute("run_id", str(run_id))
        span.set_attribute("tenant_id", str(tenant_id))
        async with tenant_session(tenant_id) as db:
            repo = RunRepository(db)
            run = await repo.get(run_id)
            if run is None or run.state != RunState.QUEUED.value:
                return

            span.set_attribute("agent_id", str(run.agent_id))
            agent = await db.get(m.Agent, run.agent_id)
            if agent is None:
                error = "agent not found"
                await merge_context(db, run, {"error": error})
                await repo.transition(run, RunState.FAILED)
                await record_activity(
                    db,
                    tenant_id=tenant_id,
                    agent_id=run.agent_id,
                    status="error",
                    message=f"Run failed: {error}",
                )
                logger.warning("run %s marked FAILED: %s", run_id, error)
                logger.info("run %s finished in state %s", run_id, RunState.FAILED.value)
                record_run_outcome(RunState.FAILED.value)
                return

            if agent.status == "pending_approval":
                # Hire gate (§5.5 A4): a pending agent must not run via ANY enqueue
                # path (delegation, cron/triggers, HTTP). The executor is the single
                # choke point all run sources funnel through, so enforce the invariant
                # here, not only at the HTTP /run endpoint.
                error = "agent awaiting hire approval"
                await merge_context(db, run, {"error": error})
                await repo.transition(run, RunState.FAILED)
                await record_activity(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    status="error",
                    message=f"{agent.name} could not run: {error}",
                )
                logger.warning("run %s marked FAILED: %s", run_id, error)
                logger.info("run %s finished in state %s", run_id, RunState.FAILED.value)
                record_run_outcome(RunState.FAILED.value)
                return

            # Honor a cancel recorded while queued (§7.2): the endpoint records a
            # run_cancellation row rather than writing agent_run (a running executor
            # holds that row's lock). The executor is the sole state mutator, so it
            # applies the cancel here -- BEFORE the runtime is ever invoked. Normal
            # return acks the stream entry, draining it.
            cancel_kind = await _cancellation_kind(db, run_id)
            if cancel_kind is not None:
                await merge_context(db, run, {"cancellation_kind": cancel_kind})
                # Free the agent -- UNLESS it is deliberately paused (budget hard-stop
                # or supervision). A budget hard-stop (§15.4 A2) freezes an idle agent
                # AND cancels its queued run; draining that run here must NOT un-freeze
                # the agent (it would also orphan pause_reason="budget", poisoning the
                # scope's idempotency check). An operator-cancelled run of a normal
                # (unpaused) agent still frees it, as before.
                if agent.pause_reason is None:
                    agent.status = "idle"
                    await publish_agent_status(agent)
                await repo.transition(run, RunState.INTERRUPTED)
                logger.info("run %s interrupted before start (cancelled while queued)", run_id)
                record_run_outcome(RunState.INTERRUPTED.value)
                return

            # ONE run per agent at a time. Nothing enforced this: the executor
            # set agent.status = "running" and never asked whether it already
            # was -- which is why two containers named for one agent were seen
            # working the same queue, each reaching for the same ticket. The
            # coalescer folds a repeat of the SAME trigger; this covers the rest
            # (a manual start during a cron run, two triggers, a redelivery).
            #
            # Deferred, not dropped: the work is real, it is merely not this
            # agent's turn yet. Leaving the stream entry unacked redelivers it
            # after the idle window, which is a retry with backoff for free.
            #
            # The agent ROW is locked first, and that lock is what makes this
            # correct with more than one worker. Without it the check and the
            # transition are two statements with a gap between them: two workers
            # both find nothing running, both start, and the agent has two
            # containers again -- the exact bug this guard exists to prevent,
            # reintroduced by the fix for it. The lock is released by the commit
            # that happens before the container runs, so it never spans the run
            # itself; after the transition, RUNNING is its own guard.
            await db.execute(select(m.Agent.id).where(m.Agent.id == run.agent_id).with_for_update())
            busy = (
                await db.execute(
                    select(m.AgentRun.id)
                    .where(
                        m.AgentRun.agent_id == run.agent_id,
                        m.AgentRun.id != run.id,
                        m.AgentRun.state == RunState.RUNNING.value,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if busy is not None:
                logger.info(
                    "run %s deferred: agent %s is already working on run %s",
                    run_id,
                    run.agent_id,
                    busy,
                )
                raise RunDeferred(str(run_id))

            await repo.transition(run, RunState.RUNNING)
            # Reflect the in-flight run on the agent so the office view stays live
            # (the old inline endpoint did this; the durable path must too).
            agent.status = "running"
            await publish_agent_status(agent)

            choice = await _resolve_mcp_connection(db, agent=agent, run_context=run.context)
            mcp_conn, mcp_error = choice.connection, choice.error
            if mcp_error is not None:
                # Loud on purpose: acting through the wrong login is worse than
                # not acting. The run FAILS with the reason on it and an error
                # activity event, exactly as an unresolvable explicit
                # mcp_connection_id has always done.
                error = mcp_error
                await merge_context(db, run, {"error": error})
                agent.status = "idle"
                await publish_agent_status(agent)
                await repo.transition(run, RunState.FAILED)
                await record_activity(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    status="error",
                    message=f"{agent.name} failed: {error}",
                )
                logger.warning("run %s marked FAILED: %s", run_id, error)
                logger.info("run %s finished in state %s", run_id, RunState.FAILED.value)
                record_run_outcome(RunState.FAILED.value)
                return

            if choice.pinned:
                # Container parity. The isolated runtime never reads `mcp_conn`;
                # its container resolves connections for itself, through
                # mcp_gateway._connections and internal_agent._load -- and the
                # only per-run signal either of those honours is this context
                # key, which both already treat as "use exactly this, nothing
                # else". Without the stamp a pinned agent silently reached its
                # DEPARTMENT's shared connection under OC8_AGENT_ISOLATION while
                # the operator's UI showed a login assigned: precisely the silent
                # borrowing this design forbids, and a break of the standing
                # in-process/container parity rule.
                #
                # ONLY for a pin. Stamping a department connection would collapse
                # _connections()'s deliberate multi-connection list back to one,
                # undoing the fix its own docstring records.
                #
                # Two pins cannot share a single mcp_connection_id -- that is
                # exactly how the second login used to vanish. The list is what
                # `_connections` reads; the single id stays as the stable
                # primary for control tools that still load one row.
                pinned_rows = choice.connections or ((mcp_conn,) if mcp_conn is not None else ())
                if pinned_rows:
                    stamp: dict[str, Any] = {
                        "mcp_connection_id": str(pinned_rows[0].id),
                        "mcp_connection_ids": [str(c.id) for c in pinned_rows],
                    }
                    await merge_context(db, run, stamp)

            task_text = str(run.context.get("task", ""))
            # Raw {bucket_key, content_type} pointers only (Task 6) -- the actual
            # bytes are fetched one layer down, at run-preamble time, so this
            # module (which drives runs generically) never has to import
            # oc8.storage.s3 itself.
            task_images_raw = run.context.get("task_images", [])
            clarifications = run.context.get("clarifications", [])
            if clarifications:
                lines = "\n".join(f"- Q: {c['question']} A: {c['answer']}" for c in clarifications)
                task_text = f"{task_text}\n\nClarifications:\n{lines}"

            # Resume after a value-gated tool approval: replace the task with a
            # focused instruction to perform exactly the approved calls (and skip
            # the rejected ones), and hand the engine the per-call verdicts so it
            # executes them instead of suspending again.
            approvals = run.context.get("resolved_tool_approvals", [])
            pre_decided: dict[str, str] = {}
            if approvals:
                from oc8.runtime.approval_resume import pre_decided_map, resume_instruction

                task_text = resume_instruction(approvals)
                pre_decided = pre_decided_map(approvals)

            # Set by delegate_task (§7) when this run is a delegated sub-task, and by
            # the wake-up when a team lead is re-queued. Absent for an ordinary run.
            parent_raw = run.context.get("parent_task_id")
            parent_task_id = uuid.UUID(str(parent_raw)) if parent_raw else None
            delegation_depth = int(run.context.get("delegation_depth", 0))
            originating_operator = run.context.get("originating_operator")

            async def _cancel_check() -> bool:
                # Per-step cancel probe (§7.2). Reads the run_cancellation table --
                # a row this transaction does NOT lock -- so under READ COMMITTED it
                # sees the cancel endpoint's committed insert at the next step
                # boundary. Signalling off the locked agent_run row is the deadlock
                # fix; see RunCancellation.
                return await _cancellation_kind(db, run_id) is not None

            async def _inbox_check() -> list[str]:
                # Per-step operator-message probe (live steering). Same unlocked
                # append-only table + READ COMMITTED pattern as _cancel_check.
                # Returns undelivered messages oldest-first and marks them
                # delivered so each is injected exactly once.
                rows = (
                    (
                        await db.execute(
                            select(m.RunMessage)
                            .where(
                                m.RunMessage.run_id == run_id,
                                m.RunMessage.delivered.is_(False),
                            )
                            .order_by(m.RunMessage.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                bodies: list[str] = []
                for row in rows:
                    bodies.append(row.body)
                    row.delivered = True
                if rows:
                    await db.flush()
                return bodies

            run_error: str | None = None
            try:
                runtime_adapter = runtime or await resolve_runtime(
                    db, tenant_id=tenant_id, agent=agent
                )
                # One heartbeat for every runtime, wrapped here rather than
                # inside each adapter: there are three of them, and a liveness
                # signal one of them forgets does not read as "unknown", it
                # reads as a death.
                async with heartbeat(tenant_id=tenant_id, run_id=run_id):
                    result = await runtime_adapter.execute(
                        db,
                        agent=agent,
                        task_text=task_text,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        mcp_conn=mcp_conn,
                        parent_task_id=parent_task_id,
                        delegation_depth=delegation_depth,
                        cancel_check=_cancel_check,
                        inbox_check=_inbox_check,
                        pre_decided=pre_decided,
                        originating_operator=(
                            str(originating_operator) if originating_operator else None
                        ),
                        task_images_raw=task_images_raw,
                    )
            except Exception as exc:  # persist failure, never crash the worker
                run_error = repr(exc)

            # The isolated runtime commits this session while its container runs
            # (to release the agent_run row lock), which expires `run`. Re-fetch a
            # live object so the transitions below don't lazy-load in a sync path.
            refreshed = await repo.get(run_id)
            if refreshed is not None:
                run = refreshed

            if run_error is not None:
                await merge_context(db, run, {"error": run_error})
                agent.status = "idle"
                await publish_agent_status(agent)
                # Failure-reassignment (§7): a genuinely failed sub-task (an
                # exception in run_agent -- the first-party runtime signals failure
                # this way, never via status="failed") must still wake its team lead
                # so the lead can re-delegate. Best-effort and defensively wrapped:
                # a wake failure must never stop the run being marked FAILED or
                # crash the worker.
                try:
                    wake_id = await _maybe_wake_parent(
                        db,
                        repo=repo,
                        tenant_id=tenant_id,
                        parent_task_id=parent_task_id,
                        delegation_depth=delegation_depth,
                        finished_agent_id=run.agent_id,
                        sub_task_label=task_text,
                        output=run_error,
                        succeeded=False,
                        mcp_conn=mcp_conn,
                        chat_session_id=(run.context or {}).get("chat_session_id"),
                        chat_channel=(run.context or {}).get("chat_channel"),
                        chat_channel_external_id=(run.context or {}).get(
                            "chat_channel_external_id"
                        ),
                    )
                    if wake_id is not None:
                        pending_runs.append(wake_id)
                except Exception:
                    logger.exception("run %s: failed to wake parent after sub-task failure", run_id)
                await sync_run_output(db, tenant_id=tenant_id, run_id=run_id)
                await repo.transition(run, RunState.FAILED)
                await record_activity(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent.id,
                    status="error",
                    message=f"{agent.name} failed: {task_text[:80]}",
                    detail=run_error[:500],
                )
                logger.warning("run %s marked FAILED: %s", run_id, run_error)
                logger.info("run %s finished in state %s", run_id, RunState.FAILED.value)
                record_run_outcome(RunState.FAILED.value)
                # This path -- an exception out of the runtime -- reached
                # FAILED without ever calling `record_assistant_reply`, so a
                # chat turn whose run crashed left the transcript with the
                # user's message and nothing after it, on every surface, for
                # ever. The generic status branch below has always recorded
                # one; this one now does too.
                if run.source == "chat":
                    from oc8.chat.service import record_assistant_reply

                    await record_assistant_reply(db, run=run, output=_CHAT_RUN_FAILED)
                    failed_sender = _chat_channel_sender_of(run)
                    if failed_sender is not None:
                        failed_channel, failed_external_id = failed_sender
                        channel_replies.append(
                            (run.tenant_id, failed_channel, failed_external_id, _CHAT_RUN_FAILED)
                        )
            else:
                pending_runs.extend(result.pending_runs)
                if result.status == "waiting_for_input":
                    from oc8.runtime.clarification import (
                        RepeatedClarification,
                        request_clarification,
                    )

                    # A clarification ask must write the open Clarification AND
                    # stash pending_question -- not just flip state -- so handle it
                    # in a dedicated branch rather than via _STATUS_TO_STATE (which
                    # only ever flips state). request_clarification itself performs
                    # the WAITING_FOR_INPUT transition; adding waiting_for_input to
                    # _STATUS_TO_STATE too would attempt a second transition on an
                    # already-transitioned run and fail the state guard.
                    run.task_id = result.task_id
                    await merge_context(
                        db,
                        run,
                        {
                            "output": result.output,
                            "toolCalls": result.tool_calls,
                            "steps": result.steps,
                            "pending_question": result.output,
                            "rendered_components": result.rendered_components,
                            "todos": result.todos,
                        },
                    )
                    try:
                        # Mirror the waiting_for_approval path: the run is
                        # suspended, not finished, so the agent is NOT idled
                        # here. (Unlike waiting_for_approval, the engine's
                        # ask_user path doesn't set agent.status itself either
                        # -- it's simply left as "running".)
                        await request_clarification(db, run=run, question=result.output)
                        logger.info("run %s waiting for input", run_id)
                        record_run_outcome(RunState.WAITING_FOR_INPUT.value)
                        # A park is not an outcome, so `record_assistant_reply`
                        # is deliberately NOT called here -- the question lives
                        # on the Clarification, and the Chat UI reads it there.
                        # The Telegram sender has no such screen in front of
                        # them, though, so without a word here their last
                        # message is answered by "Bin dran" and then silence,
                        # for ever.
                        parked_sender = _chat_channel_sender_of(run)
                        if parked_sender is not None:
                            parked_channel, parked_external_id = parked_sender
                            channel_replies.append(
                                (
                                    run.tenant_id,
                                    parked_channel,
                                    parked_external_id,
                                    _CHAT_NEEDS_INPUT,
                                )
                            )
                    except RepeatedClarification as exc:
                        # The agent asked the identical question twice on this
                        # run without acting on the answer in between. Parking
                        # a THIRD time would just hand the human the same
                        # question again -- fail loud instead, the same shape
                        # as any other run_error, so this reads in the UI and
                        # the audit trail exactly like one.
                        run_error = str(exc)
                        await merge_context(db, run, {"error": run_error})
                        agent.status = "idle"
                        await publish_agent_status(agent)
                        try:
                            wake_id = await _maybe_wake_parent(
                                db,
                                repo=repo,
                                tenant_id=tenant_id,
                                parent_task_id=parent_task_id,
                                delegation_depth=delegation_depth,
                                finished_agent_id=run.agent_id,
                                sub_task_label=task_text,
                                output=run_error,
                                succeeded=False,
                                mcp_conn=mcp_conn,
                                chat_session_id=(run.context or {}).get("chat_session_id"),
                                chat_channel=(run.context or {}).get("chat_channel"),
                                chat_channel_external_id=(run.context or {}).get(
                                    "chat_channel_external_id"
                                ),
                            )
                            if wake_id is not None:
                                pending_runs.append(wake_id)
                        except Exception:
                            logger.exception(
                                "run %s: failed to wake parent after sub-task failure", run_id
                            )
                        await sync_run_output(db, tenant_id=tenant_id, run_id=run_id)
                        await repo.transition(run, RunState.FAILED)
                        await record_activity(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            status="error",
                            message=f"{agent.name} failed: {task_text[:80]}",
                            detail=run_error[:500],
                        )
                        logger.warning("run %s marked FAILED: %s", run_id, run_error)
                        logger.info("run %s finished in state %s", run_id, RunState.FAILED.value)
                        record_run_outcome(RunState.FAILED.value)
                else:
                    run.task_id = result.task_id
                    await merge_context(
                        db,
                        run,
                        {
                            "output": result.output,
                            "toolCalls": result.tool_calls,
                            "steps": result.steps,
                            "rendered_components": result.rendered_components,
                            "todos": result.todos,
                        },
                    )
                    new_state = _STATUS_TO_STATE.get(result.status)
                    if new_state is None:
                        unknown = f"unknown run status: {result.status!r}"
                        await merge_context(db, run, {"error": unknown})
                        new_state = RunState.FAILED
                        await record_activity(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            status="error",
                            message=f"{agent.name} failed: {unknown}",
                        )
                        logger.warning("run %s marked FAILED: %s", run_id, unknown)
                    if new_state is RunState.INTERRUPTED:
                        # Carry the cancel kind onto the run so consumers see why it
                        # stopped, uniformly with the queued-skip path above.
                        kind = await _cancellation_kind(db, run_id)
                        await merge_context(
                            db, run, {"cancellation_kind": kind or "operator_interrupted"}
                        )
                    # done/failed/interrupted => the agent is free again; the engine
                    # already sets "waiting_for_approval" itself when it suspends for
                    # HITL, so don't clobber it.
                    if new_state in (RunState.DONE, RunState.FAILED, RunState.INTERRUPTED):
                        agent.status = "idle"
                        await publish_agent_status(agent)
                    # Wake the delegating lead only on a real task OUTCOME (done/failed).
                    # An interrupt is an operator action, not an outcome -- never wake,
                    # so an interrupted delegated sub-run does not re-spend its lead.
                    if new_state in (RunState.DONE, RunState.FAILED):
                        wake_id = await _maybe_wake_parent(
                            db,
                            repo=repo,
                            tenant_id=tenant_id,
                            parent_task_id=parent_task_id,
                            delegation_depth=delegation_depth,
                            finished_agent_id=run.agent_id,
                            sub_task_label=task_text,
                            output=result.output,
                            succeeded=new_state is RunState.DONE,
                            mcp_conn=mcp_conn,
                            chat_session_id=(run.context or {}).get("chat_session_id"),
                            chat_channel=(run.context or {}).get("chat_channel"),
                            chat_channel_external_id=(run.context or {}).get(
                                "chat_channel_external_id"
                            ),
                        )
                        if wake_id is not None:
                            pending_runs.append(wake_id)
                    await sync_run_output(db, tenant_id=tenant_id, run_id=run_id)
                    await repo.transition(run, new_state)
                    logger.info("run %s finished in state %s", run_id, new_state.value)
                    record_run_outcome(new_state.value)
                    if run.source == "chat" and new_state in (RunState.DONE, RunState.FAILED):
                        # A chat turn IS a run (see oc8.chat.service) so it gets the
                        # exact same guardrail/approval suspend-resume for free; this
                        # is the one place that turns its outcome back into the
                        # durable transcript message the Chat UI actually reads.
                        from oc8.chat.service import record_assistant_reply

                        await record_assistant_reply(db, run=run, output=result.output)
                        done_sender = _chat_channel_sender_of(run)
                        if done_sender is not None:
                            done_channel, done_external_id = done_sender
                            channel_replies.append(
                                (run.tenant_id, done_channel, done_external_id, result.output)
                            )
                    if new_state is RunState.WAITING_FOR_APPROVAL:
                        # Same reason as the waiting_for_input park above: the
                        # run is suspended, not finished, so nothing else tells
                        # the Telegram sender that their request is now sitting
                        # in somebody's approval queue.
                        held_sender = _chat_channel_sender_of(run)
                        if held_sender is not None:
                            held_channel, held_external_id = held_sender
                            channel_replies.append(
                                (
                                    run.tenant_id,
                                    held_channel,
                                    held_external_id,
                                    _CHAT_WAITING_FOR_APPROVAL,
                                )
                            )
                        # Close the parking race (see requeue_if_already_decided):
                        # an operator may have decided the held call while this run
                        # was still parking. Publish only after the transition
                        # below commits, mirroring the approvals endpoint.
                        if await requeue_if_already_decided(db, run=run):
                            pending_runs.append(run.id)
                            logger.info(
                                "run %s: decision for the held call arrived while "
                                "parking, re-queued immediately",
                                run_id,
                            )

        # The session committed on block exit. Only now is it safe to publish:
        # the worker's XREADGROUP can return the instant a stream entry lands, so
        # every run row must be durably visible first.
        for pending_id in pending_runs:
            await publish_run(run_id=pending_id, tenant_id=tenant_id)

        # Same reasoning as the publish loop above: a chat reply is sent
        # only once the record_assistant_reply write it reports on is
        # durably committed, never from inside the still-open transaction.
        for reply_tenant_id, reply_channel, external_id, text in channel_replies:
            await _tell_channel(
                tenant_id=reply_tenant_id, channel=reply_channel, external_id=external_id, text=text
            )


async def recover_reclaimed(message: RunMessage) -> None:
    """A redelivered entry: the previous worker claimed it and never acked.

    queued -> never started, execute normally. running -> that worker died
    mid-run; fail it and free the agent (visibly failed beats invisibly
    stuck). Terminal/missing -> duplicate delivery, nothing to do.

    "Died" needs TWO facts that agree, and until 2026-08-02 it took only one.
    The queue signal alone is not evidence of death: it says a consumer stopped
    renewing, which is also what a worker whose Redis is unreachable looks like
    while its database heartbeat runs perfectly. That worker's run is alive, in
    a container, working -- and this function would fail it as "lease lost"
    without ever asking the row. So the row is asked: a heartbeat inside
    ABANDONED_AFTER means the executor is demonstrably alive and the queue
    signal is the one that is wrong. The entry is then acked (we return
    normally), and the run keeps the reconciler as its remaining decider --
    which is the right way round, because a heartbeat that stops is the fact
    that the work stopped, and a claim that stops is only a fact about Redis.

    Resume-from-cursor is deliberately out of scope: re-executing a partially
    run agent loop without tool-call idempotency keys (§8.3) risks duplicated
    side effects.
    """
    run_id = uuid.UUID(message["run_id"])
    tenant_id = uuid.UUID(message["tenant_id"])
    async with tenant_session(tenant_id) as db:
        repo = RunRepository(db)
        # Locked, because the reconciler's sweep is the other decider and shares
        # this window: whichever of the two gets the row first writes the ending,
        # and the other waits here and then reads it. Unlocked, both read
        # `running`, both wrote, and the run was counted failed twice with the
        # second error message erasing the first.
        run = await repo.get(run_id, for_update=True)
        if run is None or run.state in (RunState.DONE.value, RunState.FAILED.value):
            return
        if run.state == RunState.RUNNING.value:
            # Read before anything writes this row: `updated_at` carries
            # onupdate=now(), so our own merge_context would refresh the very
            # fact we are about to consult.
            last_beat = run.updated_at
            if last_beat.tzinfo is None:
                last_beat = last_beat.replace(tzinfo=dt.UTC)
            if last_beat > dt.datetime.now(tz=dt.UTC) - ABANDONED_AFTER:
                logger.warning(
                    "run %s: entry reclaimed but the run is still beating "
                    "(last heartbeat %s); leaving it to its executor",
                    run_id,
                    last_beat.isoformat(),
                )
                return
            await merge_context(db, run, {"error": "lease lost: worker died mid-run"})
            agent = await db.get(m.Agent, run.agent_id)
            if agent is not None:
                agent.status = "idle"
                await publish_agent_status(agent)
            await repo.transition(run, RunState.FAILED)
            await record_activity(
                db,
                tenant_id=tenant_id,
                agent_id=run.agent_id,
                status="error",
                message=f"{agent.name if agent else 'Agent'} failed: worker died mid-run",
            )
            logger.warning("run %s marked FAILED: lease lost", run_id)
            record_run_outcome(RunState.FAILED.value)
            return
    # queued (or a waiting_* run re-queued): the session above closes here so
    # execute_run opens its own — one tenant_session per unit of work (1f55c82).
    await execute_run(message)
