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
from oc8.approvals.repo import (
    OWNER_DECIDES_TOO,
    card_department,
    department_after_person,
    direct_access_owner,
    is_member_owned,
    is_personal_card,
    personal_owner_of,
    private_card,
)
from oc8.audit import append_event
from oc8.authz.authority import granted_for_member
from oc8.authz.permissions import AGENT, APPROVAL_DECIDE_ANY, BUDGET, MANAGE, perm
from oc8.authz.scope import AgentActor, DecisionActor, HumanActor
from oc8.db.session import tenant_session
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from oc8.notifications.events import on_approval_decided
from oc8.realtime.emit import _bound_to, publish_after_commit, record_activity

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


class EffectRefused(ApprovalError):
    """Approving would apply an effect that can no longer be applied -- the agent
    builder's name taken meanwhile, its department archived, a model switched
    off. Raised AFTER the status flip: the caller must not commit. The inbox
    route answers 409 and the request transaction rolls back, so the approval
    stays pending and nothing is half-created."""

    def __init__(self, code: str) -> None:
        super().__init__(f"agent_builder:{code}")
        self.code = code


class AgentNotOffered(EffectRefused):
    """The person chose an agent their direct-access offer never named: the
    card's agents are the only ones its decision may start."""

    def __init__(self) -> None:
        super().__init__("agent_not_offered")


class AgentNotRunnable(EffectRefused):
    """The agent the person chose on their direct-access offer can no longer
    be started by them -- paused, deleted or pending since the offer, or
    their own run right reduced. Raised after the status flip like every
    `EffectRefused`: the caller rolls back and the offer stays open."""

    def __init__(self, agent_name: str) -> None:
        super().__init__("agent_not_runnable")
        self.agent_name = agent_name


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
    # Creates and configures an agent (`oc8.copilot.builder`). The same right as
    # creating one there directly: tenant-wide `agent:manage`, OR the seat's
    # agent_manage toggle in the approval's own department -- see
    # `_may_apply_the_effect`, which checks the toggle first.
    "copilot.agent_builder": perm(AGENT, MANAGE),
    # Lets a run that changed more records than a run should go on
    # (`api.mcp_gateway._stop_for_blast_radius`): an admin's call, tenant-wide
    # `approval:decide_any` -- OR the owner of the personal department the
    # alarm is filed in (`OWNER_DECIDES_TOO`, ruling U3), see
    # `_may_apply_the_effect`.
    "blast_radius": APPROVAL_DECIDE_ANY,
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
    # Writes one `direct_access_grant` for the decider's OWN conversation -- read
    # access the decider already holds through a seat (`member_may_use`,
    # re-checked when the grant is written). Who may decide is narrower than a
    # seat: only that conversation's member (`direct_access_owner`).
    "direct_access": None,
}


async def _may_apply_the_effect(
    db: AsyncSession,
    actor: DecisionActor,
    action_type: str,
    *,
    department_id: uuid.UUID | None = None,
) -> bool:
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

    `copilot.agent_builder` is the one effect a SEAT can carry: the seat's
    agent_manage toggle in the approval's own department (`department_id`),
    the same toggle `authorize_agent_write` honours for creating an agent
    there directly. Without the toggle it falls back to the table's
    tenant-wide `agent:manage`.

    `OWNER_DECIDES_TOO` (the blast-radius alarm) is the other: the owner of
    the personal department the card is filed in applies it there, from the
    screen or their own phone (`scope.personal_owned`, the ownership
    `departments.repo.is_private_to` reads), never through the Copilot acting
    as them -- an agent's assistant does not wave a runaway agent on. In any
    other department it is the table's `approval:decide_any`.
    """
    if (
        action_type in OWNER_DECIDES_TOO
        and not isinstance(actor, AgentActor)
        and department_id is not None
        and department_id in actor.scope.personal_owned
    ):
        return True
    if action_type == "copilot.agent_builder":
        if not isinstance(actor, HumanActor):
            return False
        if actor.scope.may_manage_agents(department_id):
            return True
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
    #: A run the decision started (the agent a person chose on a
    #: direct-access offer). Published by the caller after it commits, too.
    started_run_id: uuid.UUID | None = None


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
    related_links: list[dict[str, str]] | None = None,
    announce_to_member: uuid.UUID | None = None,
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
    `related_links` are the existing records the call references by argument
    (`oc8.records.links.related_links`), resolved by the caller for the same
    reason.

    `announce_to_member` makes the announcement that member's alone (see the
    branch below); None is every other approval's behaviour.

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
        related_links=related_links or [],
    )
    db.add(approval)
    await db.flush()
    personal = False
    #: An admin-kept card its personal department's owner decides too
    #: (`OWNER_DECIDES_TOO`): announced tenant-wide like any admin-kept card,
    #: and to the owner's sockets, which the tenant-wide event reaches only
    #: when the owner holds the feed.
    owner_too: uuid.UUID | None = None
    if announce_to_member is None:
        # A personal agent's card is its owner's alone (spec A4), announced
        # the way a member-owned card is: to the owner's sockets and push
        # subscriptions, and on the messenger to the owner's phone only
        # (`channels.binding.recipients`). A departed owner is told nothing,
        # and neither is anybody else; the row is kept.
        kind, owner_member_id = await card_department(db, department_id)
        personal = is_personal_card(action_type, department_kind=kind)
        if personal:
            owner = personal_owner_of(
                approval, department_kind=kind, owner_member_id=owner_member_id
            )
            if owner is not None and await _member_present(db, tenant_id, owner):
                announce_to_member = owner
        elif (
            kind == DEPARTMENT_KIND_PERSONAL
            and action_type in OWNER_DECIDES_TOO
            and owner_member_id is not None
            and await _member_present(db, tenant_id, owner_member_id)
        ):
            owner_too = owner_member_id
    member_event = {
        "approval_id": str(approval.id),
        "agent_id": str(agent_id),
        "title": approval.title,
        "detail": approval.detail,
    }
    if announce_to_member is not None:
        # A member-owned card (copilot direct access): its title and detail name
        # the person and their private purpose. Neither the tenant-wide event,
        # a push to every subscriber nor the department's messenger fan-out may
        # carry it -- only that member's own sockets hear of it.
        from oc8.realtime.bus import get_event_bus

        await get_event_bus().publish_member_event(
            tenant_id,
            [announce_to_member],
            "approval.created",
            member_event,
            source=f"oc8/approval/{approval.id}",
        )
    elif owner_too is not None:
        from oc8.realtime.bus import get_event_bus

        # No push: the tenant-wide event's push reaches the owner's
        # subscriptions already.
        await get_event_bus().publish_member_event(
            tenant_id,
            [owner_too],
            "approval.created",
            member_event,
            source=f"oc8/approval/{approval.id}",
            push=False,
        )
    # The messenger: never for a member-owned card; for a personal one only
    # while its owner is present, and then to the owner's phone alone.
    if personal:
        if announce_to_member is not None:
            await _announce(db, approval)
    elif announce_to_member is None:
        await _announce(db, approval)
    return approval


async def publish_created(
    db: AsyncSession,
    approval: m.ApprovalRequest,
    *,
    data: dict[str, Any] | None = None,
    after_commit: bool = False,
) -> None:
    """The tenant-wide `approval.created` event, and the push to every
    subscriber behind it -- the one place that sends it for a raised card.

    Silent for a card that is not the tenant's to hear of, read off the ROW
    so a freshly loaded instance answers the same as the one `raise_approval`
    returned: a member-owned card (`is_member_owned`) and a personal
    department's (`is_personal_card`), which `raise_approval` announced to
    their member alone, or to nobody once a personal department's owner has
    left. Their title and detail are that person's (spec A4).

    `data` is the envelope when the caller's differs from the usual one.
    `title`/`detail` travel in it so the push never has to re-read a row a
    fresh session may not see yet (`EventBus._push_payload`). `after_commit`
    publishes once the caller's transaction commits (`publish_after_commit`);
    the decision who hears of it is made now, on this session.
    """
    if is_member_owned(approval):
        return
    tenant_id, approval_id = approval.tenant_id, approval.id
    kind: str | None = None
    if approval.department_id is not None:
        kind = await _department_kind_or_none(db, tenant_id, approval.department_id)
        if kind is None:
            # Unreadable is not "no department": it could be a personal one.
            logger.warning(
                "could not read the department of approval %s; publishing nothing", approval_id
            )
            return
    if is_personal_card(approval.action_type, department_kind=kind):
        return
    event = (
        data
        if data is not None
        else {
            "approval_id": str(approval_id),
            "action_type": approval.action_type,
            "status": approval.status,
            "title": approval.title,
            "detail": approval.detail,
        }
    )

    async def _publish() -> None:
        from oc8.realtime.bus import get_event_bus

        await get_event_bus().publish_event(
            tenant_id, "approval.created", event, source=f"oc8/approval/{approval_id}"
        )

    if not after_commit:
        await _publish()
        return

    async def _publish_quietly() -> None:
        try:
            await _publish()
        except Exception:
            logger.warning("could not announce approval %s", approval_id, exc_info=True)

    publish_after_commit(db, _publish_quietly)


async def _department_kind_or_none(
    db: AsyncSession, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> str | None:
    """The kind of the department a card is filed in, or None when it cannot
    be read: from the caller's session while it is bound to the tenant (it may
    hold the card uncommitted), else from a fresh tenant session (a caller that
    committed has lost its binding, and RLS would show it nothing or raise) --
    the pattern of `realtime.emit._audience_for`. A lookup that fails answers
    None, and `publish_created` then publishes nothing.

    The caller's own pending writes are flushed first, OUTSIDE the guard: an
    error there is the caller's to see, not a lookup that failed."""
    await db.flush()
    try:
        if await _bound_to(db, tenant_id):
            # A savepoint, so a failed read does not abort the caller's
            # transaction.
            async with db.begin_nested():
                return (await card_department(db, department_id))[0]
        async with tenant_session(tenant_id) as fresh:
            return (await card_department(fresh, department_id))[0]
    except Exception:
        logger.warning("department %s lookup failed", department_id, exc_info=True)
        return None


async def _member_present(db: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID) -> bool:
    """The member row is live in this tenant (not offboarded). Read from the
    database, never the identity map, like `departments.personal.
    ensure_owner_present`."""
    found = await db.scalar(
        select(m.OrgMember.id).where(
            m.OrgMember.id == member_id,
            m.OrgMember.tenant_id == tenant_id,
            m.OrgMember.deleted_at.is_(None),
        )
    )
    return found is not None


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


async def expire_approval(db: AsyncSession, approval: m.ApprovalRequest) -> None:
    """Close a pending request nobody can decide any more, because what it asks
    about has expired. Not a decision: nobody decided it and no effect runs, so
    it bypasses `decide_approval` and sets `expired` (the status the table has
    always allowed). The caller holds the row locked. The queue hears of it once
    the transaction commits; a messenger withdraws the question now, exactly as
    after a decision."""
    if approval.status != "pending":
        return
    approval.status = "expired"
    approval.decided_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()
    await append_event(
        db,
        tenant_id=approval.tenant_id,
        actor_type="agent",
        actor_id=approval.agent_id,
        category="approval",
        action="approval.expire",
        resource={
            "approval_id": str(approval.id),
            "agent_id": str(approval.agent_id),
            "department_id": str(approval.department_id) if approval.department_id else None,
            "action_type": approval.action_type,
        },
        decision="expired",
    )
    tenant_id, approval_id = approval.tenant_id, approval.id

    async def _publish() -> None:
        try:
            from oc8.realtime.bus import get_event_bus

            await get_event_bus().publish_event(
                tenant_id,
                "approval.decided",
                {"approval_id": str(approval_id), "status": "expired"},
                source=f"oc8/approval/{approval_id}",
            )
        except Exception:
            logger.warning("could not announce expired approval %s", approval_id, exc_info=True)

    publish_after_commit(db, _publish)
    await _close_out(db, approval)


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


#: Payload keys a department stage never inherits from the person's card.
_PER_CARD_KEYS: Final = frozenset({"channel_handles"})


async def _raise_department_stage(db: AsyncSession, approval: m.ApprovalRequest) -> None:
    """The second card of a department-held direct write, raised once its
    person approved the first: the same call, decided by the holding
    department's approvers in the ordinary way (`may_decide`). It carries the
    run it resumes, the conversation (deleting the chat expires it), the
    read-before-write proof the person's card carried, and the person's card
    it follows (`member_approval_id`) -- which is what tells the person's
    listing that their card now waits for the department."""
    department_id = department_after_person(approval)
    if department_id is None:
        # Never tenant-wide: a stage with no department would be a card only
        # the unrestricted see. The caller checked; fail loudly if it did not.
        raise ValueError("a department stage needs the holding department")
    live = (
        await db.execute(
            select(m.Department.id).where(
                m.Department.id == department_id,
                m.Department.tenant_id == approval.tenant_id,
                m.Department.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if live is None:
        # The holding department was archived between the two stages: its
        # queue is one nobody sees, and parking the run there would wait for
        # ever. Refused like any effect that can no longer be applied: the
        # caller rolls back, the person's card stays pending (they can still
        # reject it).
        raise EffectRefused("direct_access_department_gone")
    payload = {k: v for k, v in (approval.payload or {}).items() if k not in _PER_CARD_KEYS}
    payload["direct_access"] = {
        **payload["direct_access"],
        "decider": "department",
        "member_approval_id": str(approval.id),
    }
    reason = dict(approval.reason_context or {})
    code = reason.pop("code", None)
    stage = await raise_approval(
        db,
        tenant_id=approval.tenant_id,
        agent_id=approval.agent_id,
        task_id=approval.task_id,
        action_type="tool_send",
        title=approval.title,
        detail=approval.detail or "",
        amount_text=approval.amount_text,
        payload=payload,
        department_id=department_id,
        reason_code=code if isinstance(code, str) else None,
        reason_context=reason if isinstance(code, str) else None,
        record_url=approval.record_url,
        related_links=list(approval.related_links or []),
    )
    await publish_created(db, stage, after_commit=True)


async def decide_approval(
    db: AsyncSession,
    approval: m.ApprovalRequest,
    *,
    decision: str,
    tenant_id: uuid.UUID,
    actor: DecisionActor,
    reason: str | None = None,
    option: str | None = None,
    agent_id: uuid.UUID | None = None,
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
    if is_member_owned(approval):
        # The person decides for themselves, and only they do: no seat, no
        # `decide_any`, no copilot:manage carve-out -- and not the department
        # the card happens to be filed in (R21). Same sentence as a missing row
        # for anybody else. An owned card whose member cannot be read is
        # nobody's: fail closed.
        owner = direct_access_owner(approval)
        if owner is None or actor.member.id != owner:
            raise NotYourDepartment("this approval belongs to another department")
        if isinstance(actor, AgentActor):
            # The Copilot's decide_approval acts AS the owner; letting it would
            # let the model approve its own request.
            raise NotYourSayAtAll("a direct-access approval is decided by the person, in oc8")
    elif await private_card(db, approval, actor=actor):
        # A personal agent's card is decided by its owner alone (spec A4):
        # checked before `may_decide`, which `decide_everywhere`
        # (`approval:decide_any`, `all_departments`) answers True for. Same
        # sentence as a missing row.
        raise NotYourDepartment("this approval belongs to another department")
    elif not actor.scope.may_decide(approval.department_id):
        raise NotYourDepartment("this approval belongs to another department")
    elif isinstance(actor, AgentActor) and isinstance(
        (approval.payload or {}).get("direct_access"), dict
    ):
        # The department's stage of a direct write the Copilot made: even
        # when its person is an approver there, the Copilot acting as them
        # would be approving its own call.
        raise NotYourSayAtAll("a direct-access approval is decided by people, in oc8")
    # SECOND: what `approval:decide` means depends on what is being decided, and
    # for two of the five action types it means a `:manage` act the seat
    # vocabulary is closed against. Checked in the funnel and not at the route,
    # because the action_type is on the ROW -- a gate cannot see it, which is the
    # same reason the department term lives here.
    if not await _may_apply_the_effect(
        db, actor, approval.action_type, department_id=approval.department_id
    ):
        raise NotYourSayAtAll(
            f"deciding a {approval.action_type!r} approval requires "
            f"{EFFECT_PERMISSIONS.get(approval.action_type, APPROVAL_DECIDE_ANY)}"
        )
    if decision not in _STATUS:
        raise UnknownDecision(f"unknown decision {decision!r}")
    # THIRD, the row itself, locked and read fresh: two people deciding the same
    # approval at once serialise here, and the second one reads the first one's
    # answer instead of applying the effect a second time. Only after both
    # checks above, so the lock answers nothing about a row the actor may not see.
    await db.refresh(approval, attribute_names=["status", "decided_at"], with_for_update=True)
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
    # Choosing an agent is an answer to a direct-access offer only.
    if agent_id is not None and approval.action_type != "direct_access":
        raise UnknownOption("only a direct-access offer starts a chosen agent")

    approval.status = _STATUS[decision]
    approval.decided_at = dt.datetime.now(tz=dt.UTC)
    approval.reason = reason
    # Written for the first time since the column was declared in migration 0001:
    # until there was a person table there was nothing for it to point at, so
    # "who signed this off" -- the entire value of an approval gate -- was
    # answerable only as "an operator".
    approval.decided_by = actor.member.id
    await db.flush()
    # The person behind the run is told, in this transaction: a decision whose
    # effect raises below rolls back and tells nobody anything.
    await on_approval_decided(db, approval)

    from oc8.realtime.bus import get_event_bus

    await get_event_bus().publish_event(
        approval.tenant_id,
        "approval.decided",
        {"approval_id": str(approval.id), "status": approval.status},
        source=f"oc8/approval/{approval.id}",
    )

    resumed_run: m.AgentRun | None = None
    started_run_id: uuid.UUID | None = None
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
    elif approval.action_type == "tool_send" and (
        decision == "approve" and department_after_person(approval) is not None
    ):
        # The person approved a direct write a department's own policy holds:
        # now that department's approvers are asked. The run stays parked and
        # nothing is recorded on it -- only the department's answer resumes it.
        await _raise_department_stage(db, approval)
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
    elif approval.action_type == "copilot.agent_builder":
        from oc8.copilot.builder import resolve_builder_approval

        # Applies the proposal with the approver's authority, or records the
        # rejection. Raises EffectRefused when the world changed underneath it.
        await resolve_builder_approval(
            db, approval=approval, decision=decision, reason=reason, actor=actor
        )
    elif approval.action_type == "direct_access":
        from oc8.copilot.direct_access import resolve_direct_access_approval

        # The grant (on approve) or the agent the person chose instead, and
        # either way the parked run woken and told. Raises EffectRefused when
        # the conversation, the person's right or their run right is gone.
        resumed_run = await resolve_direct_access_approval(
            db,
            approval=approval,
            decision=decision,
            agent_id=agent_id,
            # The run right is judged with the deciding person's live token,
            # like `POST /agents/{id}/run`: the owner check above made them the
            # card's own member, and a claim recorded when the offer was made
            # may have been withdrawn since.
            token_role=actor.principal.role if isinstance(actor, HumanActor) else None,
        )
        chosen = ((approval.payload or {}).get("direct_access") or {}).get("chosen_agent")
        if decision == "approve" and agent_id is not None and isinstance(chosen, dict):
            started_run_id = uuid.UUID(str(chosen["run_id"]))

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
            # What kind of card: the audit reader keeps the reason of an
            # admin-kept one (`ADMIN_KEPT_ACTIONS`) visible to admins.
            "action_type": approval.action_type,
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
    return DecisionResult(
        approval=approval,
        resumed_run_id=resumed_run.id if resumed_run else None,
        started_run_id=started_run_id,
    )
