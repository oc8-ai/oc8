"""Recording an operator's request to stop a run (§7.2), and asking at a step
boundary whether the run must stop.

Shared by the admin `POST /runs/{id}/cancel` (`run:control`) and the chat's
`POST /runs/{id}/stop` (the run's own authority, `oc8.runtime.run_inputs`):
two doors, one cancel. Each door decides WHO may stop; this decides nothing
about that and only writes the signal.

`run_stop` is the other side: the probe a container run's `/step` and the MCP
gateway's `tools/call` both ask before letting the run act again, so the order
of the checks and the kinds they record cannot drift between the two doors.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event
from oc8.runtime.run_inputs import (
    OWNERSHIP_CHANGED_KIND,
    OWNERSHIP_CHANGED_OUTPUT,
    steering_went_stale,
)
from oc8.tenants.activity import TENANT_INACTIVE_KIND, TENANT_INACTIVE_OUTPUT, tenant_may_run

if TYPE_CHECKING:
    from oc8.auth.principal import Principal

#: The cancellation_kind of a run stopped because its agent was deleted
#: (archived): written by `oc8.agents.retire`, and found by the probes below
#: when a run started at the moment of deletion has no row.
AGENT_ARCHIVED: Final = "agent_archived"
#: What a run stopped because its agent was archived reports as its output.
AGENT_ARCHIVED_OUTPUT: Final = "Cancelled: the agent was deleted"


async def record_operator_cancel(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    principal: Principal | None,
    actor_id: uuid.UUID | None,
    resource_extra: dict[str, Any] | None = None,
    cancellation_kind: str = "operator_interrupted",
) -> None:
    """Record the cancel on an UNLOCKED row. NEVER write agent_run here: a
    running executor holds that row's lock for the whole run, so a write would
    block until the run ends. The executor is the sole agent_run.state mutator
    and honors this row -- skipping a queued run before start, or stopping a
    running run at its next step boundary. Idempotent: a second cancel adds no
    row, and the first cancel keeps its kind. `cancellation_kind` names who
    asked when it was not an operator stopping this one run (`AGENT_ARCHIVED`).
    The caller has already refused a terminal run and commits.

    A run parked on an approval is the exception: no executor holds it and
    nothing would ever loop it to a step boundary, so it is woken here (see
    `_wake_parked_run`) -- the before-start check then ends it."""
    existing = (
        await db.execute(select(m.RunCancellation).where(m.RunCancellation.run_id == run.id))
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            m.RunCancellation(
                tenant_id=run.tenant_id,
                run_id=run.id,
                requested_at=dt.datetime.now(tz=dt.UTC),
                cancellation_kind=cancellation_kind,
            )
        )

    await append_event(
        db,
        tenant_id=run.tenant_id,
        actor_type="operator",
        actor_id=actor_id,
        category="run",
        action="run.cancel",
        resource={"run_id": str(run.id), "was_state": run.state, **(resource_extra or {})},
        decision="allow",
        principal=principal,
    )
    await _wake_parked_run(db, run)


logger = logging.getLogger(__name__)


#: The holds a run parks on and is resumed from. A `decision` card carries a
#: run id too, but its run never waited for it.
_PARKING_ACTIONS: Final = ("tool_send", "direct_access")


async def _parking_cards(
    db: AsyncSession, run: m.AgentRun, *, skip_locked: bool = False
) -> list[m.ApprovalRequest]:
    """The task's open parking cards, locked."""
    if run.task_id is None:
        return []
    return list(
        (
            await db.execute(
                select(m.ApprovalRequest)
                .where(
                    m.ApprovalRequest.task_id == run.task_id,
                    m.ApprovalRequest.status == "pending",
                    m.ApprovalRequest.action_type.in_(_PARKING_ACTIONS),
                )
                .with_for_update(skip_locked=skip_locked)
            )
        )
        .scalars()
        .all()
    )


async def _expire_cards_of(
    db: AsyncSession, run: m.AgentRun, cards: list[m.ApprovalRequest]
) -> None:
    """Expire those of `cards` that `run` raised; another run's stay open."""
    from oc8.approvals.service import expire_approval
    from oc8.runtime.approval_resume import run_of_approval

    for approval in cards:
        raiser = await run_of_approval(db, approval)
        if raiser is not None and raiser.id == run.id:
            await expire_approval(db, approval)


async def release_parked_run(db: AsyncSession, run: m.AgentRun) -> None:
    """At the park itself: a run that has a cancel recorded expires the cards
    it is parking on and goes back to queued, so the worker's before-start
    check ends it. `run` was just moved to waiting for approval in this
    session, so its row is ours: a card an Approve is deciding right now is
    skipped rather than waited for (the Approve waits on the run -- a
    deadlock), and that Approve then finds the run queued and leaves it to
    the cancel. The caller publishes the run after commit."""
    from oc8.runtime.repository import RunRepository
    from oc8.runtime.states import RunState

    cards = await _parking_cards(db, run, skip_locked=True)
    await _expire_cards_of(db, run, cards)
    await RunRepository(db).transition(run, RunState.QUEUED)
    await db.flush()


#: How long a Stop waits on a card or a run another writer holds before it
#: leaves the wake to the run itself (its next step, or its park).
_WAKE_LOCK_TIMEOUT: Final = "3s"


async def _wake_parked_run(db: AsyncSession, run: m.AgentRun) -> None:
    """Close the cards a parked run waits on and re-queue it, so the worker's
    before-start check honours the cancel just recorded. Left parked, the run
    waited for a decision nobody had a reason to make any more -- and in a
    chat's second turn, one nobody could make (E2E F6).

    A compare-and-set, in a savepoint: the cards are locked first (the
    Approve's order, approvals/service.py `decide`), then the run moves to
    queued only if it is still waiting for approval -- a run an Approve
    already resumed is left alone. A wait that runs out, or any error, rolls
    back only the wake: the cancel row the caller wrote still commits, and a
    run that is not parked honours it at its next step or at its park
    (executor.py, `release_parked_run`)."""
    # Deferred: the approvals service and the run queue reach the engine,
    # which reaches this module.
    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import DBAPIError

    from oc8.realtime.emit import publish_after_commit
    from oc8.runtime.intake import publish_run
    from oc8.runtime.repository import RunRepository
    from oc8.runtime.states import RunState

    fresh = await db.get(m.AgentRun, run.id, populate_existing=True)
    if fresh is None or fresh.state != RunState.WAITING_FOR_APPROVAL.value:
        return
    previous = (await db.execute(sql_text("SELECT current_setting('lock_timeout')"))).scalar_one()
    woke = False
    try:
        async with db.begin_nested():
            await db.execute(
                sql_text("SELECT set_config('lock_timeout', :t, true)"),
                {"t": _WAKE_LOCK_TIMEOUT},
            )
            pending = await _parking_cards(db, fresh)
            woke = await RunRepository(db).transition_if(
                fresh, RunState.WAITING_FOR_APPROVAL, RunState.QUEUED
            )
            if woke:
                await _expire_cards_of(db, fresh, pending)
            await db.flush()
    except DBAPIError:
        logger.warning(
            "run %s: could not wake it for its cancel now; it ends at its next step or park",
            run.id,
            exc_info=True,
        )
        woke = False
    finally:
        await db.execute(
            sql_text("SELECT set_config('lock_timeout', :t, true)"), {"t": str(previous)}
        )
    if not woke:
        return
    run_id, tenant_id = fresh.id, fresh.tenant_id

    async def _publish() -> None:
        await publish_run(run_id=run_id, tenant_id=tenant_id)

    publish_after_commit(db, _publish)


#: What an operator's cancel tells the run (and the model, through the gateway).
OPERATOR_CANCEL_OUTPUT: Final = "Run cancelled by operator."


@dataclass(frozen=True)
class RunStop:
    """Why a run must not take another step."""

    #: The cancellation kind the run ends with.
    kind: str
    #: The sentence the run (and the model) is given.
    reason: str
    #: The kind the executor can only learn from the run context: the activity
    #: gate and an ownership change leave no run_cancellation row, so the
    #: caller writes this as `cancellation_kind` on the run context. None for
    #: an operator's cancel, whose row already says so.
    recorded_kind: str | None

    @property
    def context_patch(self) -> dict[str, Any]:
        """What the caller merges into the run context; empty for an operator."""
        return {"cancellation_kind": self.recorded_kind} if self.recorded_kind else {}


async def run_stop(db: AsyncSession, run: m.AgentRun) -> RunStop | None:
    """The per-step cancel probe (§7.2) for a run driven from outside the
    in-process engine, or None when it may go on. In this order, the same as
    the executor's `_cancel_check`:

    1. an operator's run_cancellation row -- an operator cancel always wins;
    2. the run's agent was archived (`agent_archived`), also without a row;
    3. the tenant activity gate (`tenant_inactive`);
    4. the run came to act through someone else's account after it received
       a person's words (`steering_went_stale`, `ownership_changed`).

    Reads only: the caller records `RunStop.context_patch` and commits."""
    operator_kind = (
        await db.execute(
            select(m.RunCancellation.cancellation_kind).where(m.RunCancellation.run_id == run.id)
        )
    ).scalar_one_or_none()
    if operator_kind is not None:
        reason = (
            AGENT_ARCHIVED_OUTPUT if operator_kind == AGENT_ARCHIVED else OPERATOR_CANCEL_OUTPUT
        )
        return RunStop(kind=operator_kind, reason=reason, recorded_kind=None)
    if await agent_archived(db, run.agent_id):
        return RunStop(
            kind=AGENT_ARCHIVED, reason=AGENT_ARCHIVED_OUTPUT, recorded_kind=AGENT_ARCHIVED
        )
    if not await tenant_may_run(run.tenant_id):
        return RunStop(
            kind=TENANT_INACTIVE_KIND,
            reason=TENANT_INACTIVE_OUTPUT,
            recorded_kind=TENANT_INACTIVE_KIND,
        )
    if await steering_went_stale(db, run):
        return RunStop(
            kind=OWNERSHIP_CHANGED_KIND,
            reason=OWNERSHIP_CHANGED_OUTPUT,
            recorded_kind=OWNERSHIP_CHANGED_KIND,
        )
    return None


async def agent_archived(db: AsyncSession, agent_id: uuid.UUID) -> bool:
    """Whether the run's agent has been archived (or is gone) since the run
    started. A fresh column read, not the session's loaded row, so a running
    run's long transaction sees a deletion committed under READ COMMITTED."""
    deleted_at = (
        await db.execute(select(m.Agent.deleted_at).where(m.Agent.id == agent_id))
    ).one_or_none()
    return deleted_at is None or deleted_at[0] is not None
