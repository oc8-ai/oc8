"""Deleting an agent, and retiring it: nothing it set up may keep working once
it is gone.

Shared by every path that deletes an agent -- `DELETE /agents/{id}`, the
cascade of `DELETE /departments/{id}`, the Copilot's `agent.delete` and
`department.delete`, and a rejected hire -- so they cannot drift. add/flush
only: the callers own the commit.

Copilot follow-ups (rows with a `chat_session_id`) are a member's own and are
managed in the Copilot, so they are not touched here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.runtime.cancellation import AGENT_ARCHIVED, record_operator_cancel
from oc8.runtime.states import TERMINAL, RunState

if TYPE_CHECKING:
    from oc8.auth.principal import Principal

#: `trigger.last_skip_reason` of a trigger whose agent row no longer exists.
#: One whose agent was archived gets `AGENT_ARCHIVED`.
AGENT_MISSING = "agent_missing"

_WAITING = (RunState.WAITING_FOR_INPUT.value, RunState.WAITING_FOR_APPROVAL.value)


async def delete_or_archive_agent(
    db: AsyncSession,
    agent: m.Agent,
    *,
    principal: Principal | None,
    actor_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
    resource_extra: dict[str, Any] | None = None,
) -> Literal["deleted", "archived"]:
    """Hard-delete when nothing depends on the agent; archive (soft-delete)
    otherwise -- a run's `agent_id` must keep pointing at a real row. Either
    way the agent is retired (`retire_agent`)."""
    dependents = (
        await db.execute(
            select(func.count()).select_from(m.AgentRun).where(m.AgentRun.agent_id == agent.id)
        )
    ).scalar_one()
    agent_id = agent.id
    if dependents == 0:
        await db.delete(agent)
    else:
        agent.deleted_at = now or dt.datetime.now(tz=dt.UTC)
    await retire_agent(
        db,
        agent_id=agent_id,
        hard_delete=dependents == 0,
        principal=principal,
        actor_id=actor_id,
        resource_extra=resource_extra,
    )
    return "deleted" if dependents == 0 else "archived"


async def retire_agent(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    hard_delete: bool,
    principal: Principal | None,
    actor_id: uuid.UUID | None = None,
    resource_extra: dict[str, Any] | None = None,
) -> None:
    """Stop everything that would make a deleted agent work again.

    - **Hard delete** (the agent never ran): its triggers are deleted with it.
      `trigger.agent_id` has no foreign key, so a left-over trigger would fire
      into a missing agent on every tick and nobody could see it to remove it.
    - **Archive**: its triggers are disabled, not deleted, so a restore keeps
      them for the operator to turn back on deliberately. Its pending
      approvals are expired, so nobody is asked to decide for it. Every
      unfinished run gets the operator's `run_cancellation` row (kind
      `agent_archived`): a queued run is skipped at pickup, a running one
      stops at its next step boundary. A suspended run is re-queued, so the
      executor -- still the one place that ends a run -- drains it too.
    """
    own_triggers = (m.Trigger.agent_id == agent_id, m.Trigger.chat_session_id.is_(None))
    if hard_delete:
        await db.execute(delete(m.Trigger).where(*own_triggers))
        return
    # Triggers FIRST: the UPDATE holds their rows until this transaction
    # commits, so a scheduler (SKIP LOCKED) or a webhook/event fire cannot
    # start a run between the run SELECT below and the commit. A run started
    # some other way in that window is stopped by the executor's own archived
    # checks (before start, and at every step).
    await db.execute(
        update(m.Trigger)
        .where(*own_triggers, m.Trigger.enabled.is_(True))
        .values(enabled=False, last_skip_reason=AGENT_ARCHIVED)
    )
    await _expire_pending_approvals(db, agent_id=agent_id)
    open_runs = (
        (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.agent_id == agent_id,
                    m.AgentRun.state.notin_([s.value for s in TERMINAL]),
                )
            )
        )
        .scalars()
        .all()
    )
    for run in open_runs:
        await record_operator_cancel(
            db,
            run=run,
            principal=principal,
            actor_id=actor_id,
            resource_extra={"reason": AGENT_ARCHIVED, **(resource_extra or {})},
            cancellation_kind=AGENT_ARCHIVED,
        )
    await _requeue_suspended_runs(db, agent_id=agent_id)


async def _expire_pending_approvals(db: AsyncSession, *, agent_id: uuid.UUID) -> None:
    """Locked, so a concurrent decision (which locks the row and re-reads its
    status) either lands first and stands, or reads `expired`."""
    # Deferred: oc8.approvals reaches far into the agent runtime.
    from oc8.approvals.service import expire_approval

    pending = (
        (
            await db.execute(
                select(m.ApprovalRequest)
                .where(
                    m.ApprovalRequest.agent_id == agent_id,
                    m.ApprovalRequest.status == "pending",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for approval in pending:
        await expire_approval(db, approval)


async def _requeue_suspended_runs(db: AsyncSession, *, agent_id: uuid.UUID) -> None:
    """A suspended run honours its cancel only when resumed, and with its
    approval expired nobody will resume it. Re-queued here and published once
    the archive commits; the executor's archived guard then ends it before the
    runtime is reached. SKIP LOCKED: a resume already in flight wins, and the
    guard drains the run it queues just the same."""
    from oc8.realtime.emit import publish_after_commit
    from oc8.runtime.intake import publish_run
    from oc8.runtime.repository import RunRepository

    suspended = (
        (
            await db.execute(
                select(m.AgentRun)
                .where(m.AgentRun.agent_id == agent_id, m.AgentRun.state.in_(_WAITING))
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    repo = RunRepository(db)
    for run in suspended:
        await repo.transition(run, RunState.QUEUED)
        run_id, tenant_id = run.id, run.tenant_id

        async def _publish(run_id: uuid.UUID = run_id, tenant_id: uuid.UUID = tenant_id) -> None:
            await publish_run(run_id=run_id, tenant_id=tenant_id)

        publish_after_commit(db, _publish)
