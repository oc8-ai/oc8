"""Persistence + guarded state transitions for AgentRun rows."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.models import RunStateTransition
from oc8.models.run import AgentRun
from oc8.runtime.states import TERMINAL, RunState, assert_transition

logger = logging.getLogger(__name__)

#: Task states that already say how the work ended. `budget_exceeded` is the one
#: that matters: it is a REASON, not just an ending, and coarsening it to
#: `failed` would destroy the single word telling an operator to raise a budget
#: rather than debug an agent.
_TASK_SETTLED = frozenset({"done", "failed", "budget_exceeded"})

#: What a run's ending means for the task it was working. `interrupted` is an
#: operator stopping the work, which did not complete -- and `task` has no
#: `cancelled` state to be more precise with.
_TASK_STATE_FOR = {
    RunState.DONE: "done",
    RunState.FAILED: "failed",
    RunState.INTERRUPTED: "failed",
}


async def _close_task_of(session: AsyncSession, *, run: AgentRun, dst: RunState) -> None:
    """Close the task this run was working, unless something still is.

    Sits beside the claim release, and for the identical reason: this is the one
    funnel every run passes through on its way to an end, and only the
    in-process engine ever closed a task itself. Every run that went through a
    container runtime, and every run failed by the reconciler or by the executor
    catching an exception, left its task open for ever -- 66 of them on the live
    system when this was found, the oldest four days old. The board then claims
    work is being done that nobody is doing, which is the expensive direction to
    be wrong in: a lead reads it and holds other work back.

    Two things it will not do:

    - **Overwrite a settled task.** The engine says `done` or `budget_exceeded`
      first, and those are more specific than anything derivable from a run
      state.
    - **Close a task another run is still working.** A task may have several
      runs; one of them ending is not the work ending. Every task on the live
      system has exactly one run today, which is precisely why this would have
      gone unnoticed until the day it did not.
    """
    if run.task_id is None:
        return
    task = await session.get(m.Task, run.task_id)
    if task is None or task.state in _TASK_SETTLED:
        return
    still_working = (
        await session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.tenant_id == run.tenant_id,
                AgentRun.task_id == run.task_id,
                AgentRun.id != run.id,
                AgentRun.state.notin_([s.value for s in TERMINAL]),
            )
            .limit(1)
        )
    ).first()
    if still_working is not None:
        return
    task.state = _TASK_STATE_FOR.get(dst, "failed")
    await session.flush()


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        context: dict[str, Any],
        source: str = "manual",
        idempotency_key: str | None = None,
        coalesce_key: str | None = None,
        task_id: uuid.UUID | None = None,
        agent_version_id: uuid.UUID | None = None,
    ) -> AgentRun:
        run = AgentRun(
            tenant_id=tenant_id,
            agent_id=agent_id,
            state=RunState.QUEUED.value,
            context=context,
            source=source,
            idempotency_key=idempotency_key,
            coalesce_key=coalesce_key,
            task_id=task_id,
            agent_version_id=agent_version_id,
        )
        self._s.add(run)
        await self._s.flush()
        return run

    async def get(self, run_id: uuid.UUID, *, for_update: bool = False) -> AgentRun | None:
        """Load a run. `for_update` locks the row and re-reads it from the
        database rather than the identity map -- which is what a caller about to
        DECIDE this run's ending needs, since another process may be deciding it
        at the same instant and only a lock makes the two take turns."""
        if for_update:
            return await self._s.get(AgentRun, run_id, with_for_update=True)
        return await self._s.get(AgentRun, run_id)

    async def transition(self, run: AgentRun, dst: RunState) -> None:
        src = RunState(run.state)
        if dst in TERMINAL:
            # Re-read the state under a row lock, because `run.state` is only
            # what THIS session read, possibly minutes ago. Two closers now
            # legitimately agree about the same run -- the reconciler's sweep
            # and the queue's reclaim share one window since 2026-08-02 -- and
            # they run in different processes, so the check below was comparing
            # a stale copy. Both would pass it, both would commit (the UPDATE
            # carries no state predicate), and the run would be counted failed
            # twice, have its claims released twice, its task closed twice, and
            # whichever error message landed second would erase the other.
            #
            # The lock also SERIALISES them: the second waits here, then reads
            # the terminal state the first committed and leaves. No new lock
            # order is introduced -- every caller that reaches a terminal
            # transition has already written this row (merge_context), so this
            # session holds the row exclusively before it asks.
            committed = (
                await self._s.execute(
                    select(AgentRun.state).where(AgentRun.id == run.id).with_for_update()
                )
            ).scalar_one_or_none()
            if committed is not None:
                src = RunState(committed)
        if src in TERMINAL and dst in TERMINAL:
            # Somebody else finished this run first. The reconciler can close a
            # run whose heartbeat lapsed while its worker is still inside the
            # runtime call; when that worker returns and marks the run failed,
            # the transition is failed -> failed, which is not allowed and
            # raised straight out of the worker loop -- leaving the message
            # unacked and redelivered for ever. A race between two writers who
            # AGREE is not an error, and the first verdict stands.
            logger.info(
                "run %s is already %s; leaving it rather than marking it %s",
                run.id,
                src.value,
                dst.value,
            )
            return
        assert_transition(src, dst)
        run.state = dst.value
        await self._s.flush()
        self._s.add(
            RunStateTransition(
                id=uuid.uuid4(),
                tenant_id=run.tenant_id,
                run_id=run.id,
                from_state=src.value,
                to_state=dst.value,
                at=dt.datetime.now(dt.UTC),
            )
        )
        if dst in TERMINAL:
            # Whatever records this run was working, it is no longer working
            # them. Released HERE because this is the single funnel every run
            # passes through on its way to an end -- releasing at the end of the
            # happy path would leave a failed run holding a ticket for ever.
            from oc8.agent.claims import release_run_claims

            await release_run_claims(self._s, tenant_id=run.tenant_id, run_id=run.id)
            # And the task it was working, for the same reason and in the same
            # place. See _close_task_of.
            await _close_task_of(self._s, run=run, dst=dst)
        from oc8.realtime.bus import get_event_bus

        await get_event_bus().publish_event(
            run.tenant_id,
            "run.status",
            {
                "run_id": str(run.id),
                "agent_id": str(run.agent_id),
                "state": run.state,
                "phase": run.phase,
            },
            source=f"oc8/run/{run.id}",
        )
