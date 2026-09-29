"""Deciding an approval, in one place, whatever asked.

Extracted from the inbox endpoint because a decision is about to arrive from
somewhere other than the UI (§5.6: an approval channel — Telegram, WhatsApp).
The rule that makes that safe is that a channel is a VIEW and an INPUT DEVICE,
never a second record: it renders the same `ApprovalRequest` and submits through
this function, so RBAC, the audit line, the resume path and the "already
decided" answer are identical whatever the answer came in on.

The alternative — a channel writing the row itself — is a parallel approval
system that agrees with the first one until the day it does not.

HTTP lives in the caller. This raises domain errors so a Telegram callback and a
POST get the same outcome and phrase it in their own vocabulary.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.authz.authority import granted_for_member
from oc8.authz.permissions import AGENT, APPROVAL_DECIDE_ANY, BUDGET, MANAGE, perm
from oc8.authz.scope import DecisionActor, HumanActor
from oc8.realtime.emit import record_activity

Verdict = Literal["approve", "reject"]

logger = logging.getLogger(__name__)

_STATUS: dict[str, str] = {"approve": "approved", "reject": "rejected"}


class ApprovalError(Exception):
    """Something about the decision itself is wrong."""


class UnknownDecision(ApprovalError):
    pass


class _Derive:
    """The type of `DERIVE`. A class rather than `object()` so the signature can
    be typed and mypy can tell "not passed" apart from "passed None"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "DERIVE"


#: "Work out which department this belongs to from the agent that raised it."
#:
#: NOT `None`, and the difference is the whole point: `None` is a real, legal
#: value meaning TENANT-WIDE, visible only to the unrestricted. Had the default
#: been `None`, every one of the eight raisers that does not think about
#: departments would have filed its approval company-wide -- invisible to every
#: seat-holder, undecidable by the person it was raised for, and nothing would
#: have errored. Exactly one caller passes `None` on purpose
#: (`metering/budget.py`'s tenant-scope incident) and a test asserts it is the
#: only one.
DERIVE = _Derive()


class AlreadyDecided(ApprovalError):
    """Not an error condition so much as a race with a normal outcome.

    Several people can be looking at the same request in the inbox, on Telegram
    and on WhatsApp at once. Whoever answers first decides; everybody after that
    deserves a sentence saying who and when, not a failure.
    """

    def __init__(self, status: str, decided_at: dt.datetime | None) -> None:
        self.status = status
        self.decided_at = decided_at
        super().__init__(f"already {status}")


class UnknownOption(ApprovalError):
    pass


class NotYourSayAtAll(ApprovalError):
    """The actor may decide in this department, but not THIS kind of approval.

    `approval:decide` is not one right: it is polymorphic on `action_type`, and
    two of the five things it can resolve are management acts gated elsewhere.
    See `EFFECT_PERMISSIONS`.

    Distinct from `NotYourDepartment` because it is answerable out loud: the
    caller has already been admitted to the row by `may_decide`, so naming the
    permission tells them nothing they could not see, and a silent 404 here would
    read as the approval having vanished.
    """


#: What approving an approval of this `action_type` ACTUALLY DOES, expressed as
#: the tenant-wide permission that same effect is gated on everywhere else.
#:
#: The hole this closes, reproduced end to end before it was written: a Head of
#: Sales holding one `dept_approver` seat and the token role `member` -- who is
#: 403'd at `GET /budgets` and holds neither `budget:manage` nor `agent:manage` --
#: could `POST /approvals/{id}/decision` on a budget incident and get
#: `budget.override_until = <next month>` plus a resumed scope, i.e. lift his own
#: department's token cap for the rest of the month; and could approve a
#: `hire_agent` request, moving an agent from `pending_approval` into service, or
#: reject it and soft-delete it. Both effects are `:manage`-class and neither is
#: in `SEAT_PERMISSIONS`. This slice is also what routed the first one into his
#: queue: `metering/budget.py` now files the incident under the breaching
#: department, where before it carried no department at all and the route was
#: `require_permission(APPROVAL_DECIDE)`.
#:
#: `tests/approvals/test_a_seat_cannot_apply_a_management_effect.py::
#: test_every_action_type_the_funnel_dispatches_on_is_classified` sweeps
#: `decide_approval`'s own dispatch chain out of the source and fails for any
#: `action_type` missing from this table, so a sixth kind of approval cannot be
#: added without somebody saying what approving it costs.
EFFECT_PERMISSIONS: Final[dict[str, str | None]] = {
    # Sets `budget.override_until` and un-pauses the frozen scope
    # (`metering/budget.py::resolve_budget_incident`) -- the same act as
    # `PUT /budgets`, which is `budget:manage`.
    "budget_incident": perm(BUDGET, MANAGE),
    # Activates a pending agent, or soft-deletes it (`agents/hire.py`). Creating
    # one needs `agent:manage`; bringing it into service is the same authority.
    "hire_agent": perm(AGENT, MANAGE),
    # None means "a seat is enough", and each one is a claim, not a default:
    #
    # `tool_send` resumes the run that is holding the call -- the 3000-EUR gate,
    # the whole reason the workspace exists. `decision` starts a follow-up run for
    # the agent that asked; requiring `run:start` would refuse the flagship screen
    # its flagship act. `memory_write` flips one `memory_record.status` belonging
    # to the agent that raised it, and that agent stands in the department the
    # seat covers.
    "tool_send": None,
    "decision": None,
    "memory_write": None,
}


async def _may_apply_the_effect(db: AsyncSession, actor: DecisionActor, action_type: str) -> bool:
    """Does this actor hold what approving THIS kind of approval actually costs?

    An unknown `action_type` is refused for anybody without `approval:decide_any`
    -- fail-closed, because an approval kind this table does not know is one
    nobody has said the cost of. `EFFECT_PERMISSIONS` is swept against the
    dispatch chain by a test, so reaching that branch means a raiser was added
    with no handler at all.

    A `ChannelActor` never holds a tenant-wide permission: a messenger message
    carries no token, so there is no role to read. A CEO answering a budget
    incident from his phone is therefore refused and has to open the screen. That
    is a real narrowing, and it is new-in-this-slice work either way -- before
    this slice a budget incident was built by hand and announced on no channel at
    all, so no phone ever saw one.

    RESOLVED, and no longer `role_has(actor.principal.role, required)`. This is
    the check the seat slice added after a seat-holder lifted his own
    department's token cap through an approval he was allowed to decide; read off
    the token, a demoted administrator walks straight back through it holding
    `budget:manage` from a claim an assignment was supposed to have replaced.
    Async for that one reason -- the answer is a row now, and the alternative
    (carrying the resolved set on `HumanActor`) would put a default on the actor
    that a future door could satisfy by leaving it out.
    """
    required = EFFECT_PERMISSIONS.get(action_type, APPROVAL_DECIDE_ANY)
    if required is None:
        return True
    if not isinstance(actor, HumanActor):
        return False
    granted, _source = await granted_for_member(db, actor.principal, actor.member)
    return required in granted


class NotYourDepartment(ApprovalError):
    """The actor may not decide in the department this approval belongs to.

    Defence in depth, and expected to be unreachable through either door that
    exists today: the inbox loads through `approvals.repo.load_for_actor` and
    404s first, and the messenger checks `may_decide` before it gets here. It is
    raised anyway because the door written next year is the one this funnel
    exists for -- and a funnel that trusts its callers to have checked is a
    funnel that will one day be called by somebody who did not.

    Both callers must answer it with the same words they use for "no such
    approval". A refusal that says "another department's" is a refusal that
    confirms the approval exists.
    """


@dataclass
class DecisionResult:
    approval: m.ApprovalRequest
    #: Set when a suspended run was re-queued. The CALLER publishes it, after it
    #: commits: a stream entry whose run row is not yet visible is a run a worker
    #: picks up and cannot find.
    resumed_run_id: uuid.UUID | None = None


async def raise_approval(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    action_type: str,
    title: str,
    detail: str = "",
    task_id: uuid.UUID | None = None,
    amount_text: str | None = None,
    payload: dict[str, Any] | None = None,
    expires_at: dt.datetime | None = None,
    department_id: uuid.UUID | _Derive | None = DERIVE,
    reason_code: str | None = None,
    reason_context: dict[str, Any] | None = None,
    record_url: str | None = None,
) -> m.ApprovalRequest:
    """Create a request for a human, and tell the people who can answer it.

    The single funnel for raising one, for the same reason `decide_approval` is
    the single funnel for answering: approvals were built in eight places, and
    anything that has to happen for all of them -- announcing on a messenger
    today, an escalation timer tomorrow -- would otherwise be eight edits, seven
    of which get made.

    Announcing here rather than after the caller commits is a deliberate
    trade with one honest cost: a transaction that rolls back afterwards leaves a
    message pointing at an approval that never existed. Pressing its button is
    then refused (core resolves the approval and finds nothing), which is
    confusing but safe -- whereas announcing post-commit would mean every caller
    remembering to, and the ones that forget fail silently. The window is small
    and the failure is visible; the alternative fails quietly and forever.

    `reason_code`/`reason_context` are the PDP `Decision`'s structured "why"
    (see `authz.pdp.Decision`), stored together as `ApprovalRequest.reason_context`.
    Leave both None for an approval not raised from an `authorize_tool_call`
    decision -- `detail`/`amount_text` remain the only "why" for those.

    `record_url` is the already-resolved deep link to the record being acted
    on (§6). Resolved by the CALLER, not here: the URL shape belongs to the
    tool pack's manifest and the base URL to the connection's own config, and
    this funnel knows about neither -- see `oc8.approvals.record_url`. None
    for every raiser that has no record in hand, which is most of them.

    `department_id` defaults to `DERIVE`, which reads it off the agent. It is
    pinned here, at the moment the question is asked, rather than joined through
    `Agent` at read time: moving an agent between departments must not drag its
    pending approvals into a queue nobody in that department ever saw them
    arrive in -- somebody is looking at the old queue right now. `Task` is
    deliberately never consulted: `task_id` is nullable (a held tool call raised
    outside a task would land nowhere) while `Agent.department_id` is NOT NULL.
    """
    if isinstance(department_id, _Derive):
        department_id = await _department_of(db, tenant_id=tenant_id, agent_id=agent_id)
    approval = m.ApprovalRequest(
        tenant_id=tenant_id,
        agent_id=agent_id,
        department_id=department_id,
        task_id=task_id,
        action_type=action_type,
        title=title,
        detail=detail,
        amount_text=amount_text,
        payload=payload or {},
        expires_at=expires_at,
        status="pending",
        reason_context={"code": reason_code, **(reason_context or {})}
        if reason_code is not None
        else None,
        record_url=record_url,
    )
    db.add(approval)
    await db.flush()
    await _announce(db, approval)
    return approval


async def _department_of(
    db: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> uuid.UUID | None:
    """The department the raising agent stands in.

    A `select(...)` rather than `db.get`, which would hand back an identity-map
    copy without issuing SQL -- and the copy an agent's own runtime is holding
    is the one whose `department_id` may have just been reassigned in this very
    transaction.

    Returns None (tenant-wide) only when the agent cannot be read: `agent_id`
    carries no foreign key, so a corrupted or cross-tenant value would otherwise
    raise here and take down a tool call that only wanted to ask a human. NULL
    is the fail-SAFE direction -- the approval is then visible to the
    unrestricted alone, rather than filed under some other tenant's department.
    """
    return (
        await db.execute(
            select(m.Agent.department_id).where(
                m.Agent.id == agent_id, m.Agent.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


async def _announce(db: AsyncSession, approval: m.ApprovalRequest) -> None:
    """Best-effort, and never in the caller's way. A messenger being down must
    not stop an approval from existing: the inbox is the record."""
    try:
        from oc8.channels.dispatch import announce
        from oc8.channels.registry import channels_for_tenant

        channels = await channels_for_tenant(db, tenant_id=approval.tenant_id)
        if not channels:
            return
        handles = await announce(db, approval, channels=channels)
        if handles:
            # Kept on the approval so `close_out` can withdraw the exact messages
            # it sent, rather than guessing which chat holds which.
            approval.payload = {**(approval.payload or {}), "channel_handles": handles}
            await db.flush()
    except Exception:
        logger.warning("could not announce approval %s on any channel", approval.id, exc_info=True)


async def _close_out(db: AsyncSession, approval: m.ApprovalRequest) -> None:
    """Withdraw the question from every chat it is still standing in.

    Without this it keeps sitting there looking open, and the next person to tap
    it is told they were too late by a system that could have said so an hour
    earlier. Best-effort for the same reason announcing is: the decision is
    already recorded and must not be undone by a messenger.
    """
    try:
        from oc8.channels.dispatch import close_out
        from oc8.channels.registry import channels_for_tenant

        channels = await channels_for_tenant(db, tenant_id=approval.tenant_id)
        if not channels:
            return
        raw = (approval.payload or {}).get("channel_handles")
        handles = raw if isinstance(raw, dict) else None
        await close_out(db, approval, channels=channels, outcome=approval.status, handles=handles)
    except Exception:
        logger.warning(
            "could not withdraw approval %s from its channels", approval.id, exc_info=True
        )


async def _abandon_unresumable(db: AsyncSession, *, approval: m.ApprovalRequest) -> None:
    """Close out a task whose held tool call can never execute.

    Terminal rather than left waiting, because "waiting_for_approval" claims a
    human still has to act -- and one already did. The operator gets a warning in
    the feed too: approving something that silently did nothing is exactly the
    kind of thing a person needs told, not left to discover on the board.
    """
    if approval.task_id is None:
        return
    task = await db.get(m.Task, approval.task_id)
    if task is None or task.state != "waiting_for_approval":
        return

    # A run that is still alive will honour the decision on its next attempt --
    # a long-lived runtime driving tools through the MCP gateway never leaves
    # RUNNING, because nothing exits to transition it. Burying its task here would
    # kill work that is about to succeed.
    from oc8.runtime.states import RunState

    live = (
        await db.execute(
            select(m.AgentRun).where(
                m.AgentRun.task_id == task.id,
                m.AgentRun.state.in_((RunState.RUNNING.value, RunState.QUEUED.value)),
            )
        )
    ).first()
    if live is not None:
        return

    task.state = "failed"
    agent = await db.get(m.Agent, approval.agent_id)
    await record_activity(
        db,
        tenant_id=approval.tenant_id,
        agent_id=approval.agent_id,
        status="warning",
        message=(
            f"{agent.name if agent is not None else 'An agent'}'s run could not be "
            f"resumed after your decision — the held action never ran"
        ),
    )


async def decide_approval(
    db: AsyncSession,
    approval: m.ApprovalRequest,
    *,
    decision: str,
    tenant_id: uuid.UUID,
    actor: DecisionActor,
    reason: str | None = None,
    option: str | None = None,
) -> DecisionResult:
    """Record a human's answer and set whatever it unblocks in motion.

    `actor` is a required keyword with NO default, and that is the whole of why
    the department term holds. Until this slice the nearest parameter was
    `principal: Principal | None = None`, so a door that forgot it compiled, ran,
    and decided; now a caller that omits it is a `TypeError` at call time and a
    mypy error in CI. `principal` and `via` are REMOVED rather than deprecated --
    left behind, they are two ways to satisfy the signature while saying nothing
    about scope.

    The scope is DERIVED from the actor, never handed in: `scope_for_principal` /
    `scope_for_binding` are the only producers of one, and a `DepartmentScope`
    cannot be constructed anywhere else.

    `actor.via` says which channel carried it ("inbox", "telegram", …) and is
    written into the audit line: when a refund was approved from a phone at
    23:40, the trail should say so rather than leave it looking like somebody was
    at a desk.
    """
    # FIRST, and before the row's own state is looked at: `AlreadyDecided` names
    # the status and the time, so checking it first would answer "does this
    # approval exist and when was it answered" for a department the caller may
    # not see.
    if not actor.scope.may_decide(approval.department_id):
        raise NotYourDepartment("this approval belongs to another department")
    # SECOND: what `approval:decide` means depends on what is being decided, and
    # for two of the five action types it means a `:manage` act the seat
    # vocabulary is closed against. Checked in the funnel and not at the route,
    # because the action_type is on the ROW -- a gate cannot see it, which is the
    # same reason the department term lives here.
    if not await _may_apply_the_effect(db, actor, approval.action_type):
        raise NotYourSayAtAll(
            f"deciding a {approval.action_type!r} approval requires "
            f"{EFFECT_PERMISSIONS.get(approval.action_type, APPROVAL_DECIDE_ANY)}"
        )
    if decision not in _STATUS:
        raise UnknownDecision(f"unknown decision {decision!r}")
    if approval.status != "pending":
        raise AlreadyDecided(approval.status, approval.decided_at)

    # Imported here, not at module scope: `oc8.agent` imports this module for
    # `raise_approval`, so a top-level import would be a cycle that only shows
    # itself when the app starts -- tests happen to import in the other order.
    from oc8.agent.decision_followup import option_labels

    # Validated BEFORE anything is written: a typo in the option must not leave a
    # decided approval whose instruction nobody can act on.
    if approval.action_type == "decision" and option is not None:
        if option not in option_labels(approval):
            raise UnknownOption(f"unknown option {option!r} for this decision")

    approval.status = _STATUS[decision]
    approval.decided_at = dt.datetime.now(tz=dt.UTC)
    approval.reason = reason
    # Written for the first time since the column was declared in migration 0001:
    # until there was a person table there was nothing for it to point at, so
    # "who signed this off" -- the entire value of an approval gate -- was
    # answerable only as "an operator".
    approval.decided_by = actor.member.id
    await db.flush()

    from oc8.realtime.bus import get_event_bus

    await get_event_bus().publish_event(
        approval.tenant_id,
        "approval.decided",
        {"approval_id": str(approval.id), "status": approval.status},
        source=f"oc8/approval/{approval.id}",
    )

    resumed_run: m.AgentRun | None = None
    if approval.action_type == "memory_write":
        from oc8.memory.router import resolve_memory_write_approval

        await resolve_memory_write_approval(db, approval_request=approval, decision=decision)
    elif approval.action_type == "budget_incident":
        from oc8.metering import resolve_budget_incident

        await resolve_budget_incident(db, approval_request=approval, decision=decision)
    elif approval.action_type == "hire_agent":
        from oc8.agents.hire import resolve_hire_agent

        await resolve_hire_agent(db, approval_request=approval, decision=decision)
    elif approval.action_type == "decision":
        from oc8.agent.decision_followup import carry_out_decision

        # The agent did not wait for this, so there is no run to resume: the
        # decision becomes a NEW run that carries it out. A rejection travels
        # too -- somebody still has to tell the customer no.
        await carry_out_decision(
            db, approval=approval, decision=decision, reason=reason or "", option=option
        )
        # enqueue_run COMMITS, and the RLS GUC is transaction-local: everything
        # below (the audit append, the DTO) would otherwise run unbound and fail
        # the uuid cast. Same trap as the mid-run commit in runtime/isolated.py.
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
    elif approval.action_type == "tool_send":
        # Resume the suspended run: on approve it executes the held tool call, on
        # reject it moves past it. The run is re-queued and published by the caller.
        from oc8.runtime.approval_resume import resolve_tool_approval

        resumed_run = await resolve_tool_approval(db, approval=approval, decision=decision)
        if resumed_run is None:
            # Nothing to resume -- the run is gone, already moved on, or was never
            # linked (runs suspended before the task-row fix look like this). The
            # decision still stands and is audited: an operator did decide. But the
            # held action will never execute, so the task must NOT be left sitting
            # in waiting_for_approval, where it reads as "still waiting on a human"
            # forever and quietly fills the board with undead work.
            await _abandon_unresumable(db, approval=approval)

    await _close_out(db, approval)
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="operator",
        # Was unconditionally None, which is why an audit of a 23:40 phone
        # approval could say "an operator" and never which one.
        actor_id=actor.member.id,
        category="approval",
        action=f"approval.{decision}",
        resource={
            "approval_id": str(approval.id),
            "agent_id": str(approval.agent_id),
            # Whose queue this was in, and whose task it belonged to. Both are on
            # the row and neither was in the trail: "who may decide this" is the
            # first question asked about a decision afterwards.
            "department_id": str(approval.department_id) if approval.department_id else None,
            "task_id": str(approval.task_id) if approval.task_id else None,
            "member_id": str(actor.member.id),
            "via": actor.via,
        },
        decision=approval.status,
        reason=reason or None,
        # Only an inbox decision has a token behind it. A messenger message is not
        # an authentication event, so nothing is invented here -- attribution for
        # that door is `member_id`, which is exactly why a binding with
        # `member_id IS NULL` decides nothing.
        principal=actor.principal if isinstance(actor, HumanActor) else None,
    )
    return DecisionResult(approval=approval, resumed_run_id=resumed_run.id if resumed_run else None)
