"""Close runs that nothing is running any more.

There are two ways a run ends up abandoned, and neither is covered by the
machinery that already exists:

* The worker is killed mid-run. Its stream entry stays unacked, and after the
  idle window another worker reclaims it and fails the run. That path works.
* The worker ACKED the entry and then the work died anyway -- the container was
  killed, the host restarted, the process wedged. There is no entry left to
  reclaim, so nobody ever looks at that run again.

The second case leaves a row in `running` for ever. Observed live, 2026-07-28:
nine runs marked running with two containers alive, three of the rows five
hours old. The cost is not the row. It is that the agent looks permanently busy
on its own board, its containers are invisible to the container reaper (which
only removes containers of FINISHED runs), and the run's task never reaches
anyone -- a failure nobody is told about is worse than a failure.

This is the inverse sweep to sandbox.reaper: that one removes containers whose
run has finished, this one finishes runs that stopped reporting in. It decides
on ONE fact, the run's own heartbeat, and it is worth being plain about that
because this file used to promise a second one -- "no container is labelled with
its id" -- that `close_abandoned_runs` never checked and never could: liveness
here is REPORTED, because a second worker on a second host sees none of the
first host's containers and would call its live runs dead.

The rule is deliberately narrow, for the same reason the container reaper's is:
the dangerous half is not leaving a dead run open, it is killing a live one. So
the threshold is about missed REPORTS and never about elapsed work: a run is
closed only once whatever executes it has stopped saying it is alive for
`ABANDONED_AFTER` -- twenty consecutive missed beats. A run that keeps beating is
left alone however long it takes; nothing in THIS file stops an agent working
for an hour. (Its runtime may: a container runtime caps its own work at
`agent_max_steps * 60`. That is a cap on work, decided where the work is, and
not a liveness guess made from outside.)

Length is not evidence of death, and this comment used to claim otherwise: it
said the window was "far longer than any run this system produces". It is not,
and no such number can be named honestly -- run 019fc303 (Nora, real CRM work,
2026-08-02) was five minutes in and still going. The queue's reclaim was built on
that false sentence and killed her run for being slow.

The same window governs the queue: `oc8.runtime.worker` renews its claim on the
run's stream entry on the SAME beat and treats a claim unrenewed for this SAME
`ABANDONED_AFTER` as a dead worker. One window, two deciders -- and they are not
the same fact. This one knows whether the WORK is reporting; the queue's knows
whether a consumer is talking to Redis, which a partition can silence while the
work runs perfectly. So the queue's signal may not kill a run by itself:
`executor.recover_reclaimed` reads this heartbeat before it believes a reclaim.
Two facts, and they have to agree -- which is the rule this module has always
stated and, until 2026-08-02, only half kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import AsyncIterator
from functools import partial

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oc8 import models as m
from oc8.config import get_settings
from oc8.db.session import tenant_session
from oc8.observability import record_run_outcome
from oc8.realtime.emit import publish_after_commit, publish_agent_status
from oc8.runtime.queue import get_run_queue
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import merge_context
from oc8.runtime.states import RunState
from oc8.triggers.scheduler import list_active_tenant_ids, list_runnable_tenant_ids

logger = logging.getLogger(__name__)

#: How often an executing run reports that it is still alive -- to its database
#: row here, and to its stream entry in oc8.runtime.worker. One beat, both places.
HEARTBEAT_SECONDS = 30.0

#: How long that may stop before the run counts as abandoned. Twenty missed
#: beats: long enough that a slow database, a paused host or a stop-the-world
#: pause is never mistaken for a death, short enough that a run nothing is
#: executing any more frees its agent within a coffee break rather than at the
#: next deploy. Public because the worker applies the same window to the queue.
ABANDONED_AFTER = dt.timedelta(minutes=10)

#: Said on the run itself, because "failed" with no reason reads as the agent's
#: failure when it is the platform's.
REASON = (
    "abandoned: whatever was executing this stopped reporting in. "
    "The run was closed by the reconciler; nothing was lost that had been "
    "written, and the work can be started again."
)


async def close_abandoned_runs(
    *, now: dt.datetime | None = None
) -> list[uuid.UUID]:
    """Fail every run whose executor has gone silent. Returns the ids closed.

    Never raises: this runs on the worker's own timer, and one bad sweep must
    not stop it taking work.
    """
    cutoff = (now or dt.datetime.now(tz=dt.UTC)) - ABANDONED_AFTER

    closed: list[uuid.UUID] = []
    for tenant_id in await list_active_tenant_ids():
        try:
            async with tenant_session(tenant_id) as db:
                stale = (
                    (
                        await db.execute(
                            # Locked as they are selected, and a row somebody
                            # else already holds is left for them. Since
                            # 2026-08-02 this sweep and the queue's reclaim share
                            # one window, so for the first time they routinely
                            # agree about the same run at the same moment; with
                            # an unlocked read both saw `running`, both wrote,
                            # and the second error message erased the first.
                            # SKIP LOCKED rather than a wait, because a run
                            # somebody is already closing needs nothing from us.
                            select(m.AgentRun)
                            .where(
                                m.AgentRun.state == RunState.RUNNING.value,
                                m.AgentRun.updated_at < cutoff,
                            )
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                repo = RunRepository(db)
                for run in stale:
                    await merge_context(db, run, {"error": REASON})
                    agent = await db.get(m.Agent, run.agent_id)
                    if agent is not None and agent.status == "running":
                        # Freeing the agent is the point: while it reads as busy
                        # the office view lies, and a lead may hold work back.
                        # Announced once this commits, never for a rolled-back write.
                        agent.status = "idle"
                        publish_after_commit(db, partial(publish_agent_status, agent))
                    await repo.transition(run, RunState.FAILED)
                    record_run_outcome(RunState.FAILED.value)
                    closed.append(run.id)
        except Exception:
            logger.exception("reconciler: sweep failed for tenant %s", tenant_id)
    if closed:
        logger.warning(
            "reconciler: closed %d abandoned run(s): %s",
            len(closed),
            ", ".join(str(r) for r in closed),
        )
    return closed


#: Agent statuses whose queued runs the worker REFUSES to start: only the hire
#: gate (`execute_run` fails a run of an agent that has not been hired). A
#: lost publish for such a run is left queued until the hold ends, when it
#: becomes an ordinary candidate. `paused` and `stopped` are deliberately NOT
#: here: the worker runs those agents' queued runs (`stopped` is the default
#: status of a new agent), so their stranded runs are recovered like any other.
HELD_AGENT_STATUSES = ("pending_approval",)


async def recover_stranded_runs() -> list[uuid.UUID]:
    """Publish again every queued run whose stream entry never landed. Returns
    the ids republished.

    Every run source commits the row and THEN publishes it (the worker may load
    the run the instant the entry exists, so it has to be durable first). A
    crash, a Redis outage or a raising publish between the two leaves a row in
    `queued` that nothing will ever start: the queue's reclaim only sees entries
    that exist, and `close_abandoned_runs` only sweeps `running`.

    Stranded is decided on the run's `updated_at`, the existing column -- no
    new one. A queued run's `updated_at` is when it was created or re-queued
    (resuming from approval or clarification moves it); coalescing a repeat
    fire onto it deliberately does NOT move it (see `intake.enqueue_run`). This
    sweep moves it on every candidate it looks at, so the same run is
    considered at most once per `stranded_run_after_seconds` however often the
    sweep ticks. Both the cutoff and the marker are the DATABASE's clock, so a
    worker host with a skewed clock cannot make every run look stranded. Nothing
    else reads `updated_at` on a queued run: the heartbeat it doubles as only
    means something on a `running` one.

    LEFT ALONE, because the worker would deliberately not start them or
    because their entry is not lost:

    * a run whose entry is still in the stream's backlog, undelivered -- under
      load that can be older than any threshold, and republishing a backlog
      doubles it. Its marker still moves, so a long backlog cannot pin the
      batch to the same runs every tick;
    * a run of an agent awaiting hire (`HELD_AGENT_STATUSES`);
    * a run of an agent that is already running another run. The worker defers
      it (one run per agent), and a deferred entry stays pending and comes back
      by itself; a republished one would defer too, so for as long as the agent
      is busy every tick would add an entry that never goes away;
    * every run of an organisation that may not run (`list_runnable_tenant_ids`).

    Idempotent where it matters, in the worker: a run published twice executes
    once, because `execute_run` starts only a run it finds `queued` under the
    row lock. Rows are taken with SKIP LOCKED, so two workers sweeping at once
    split the work instead of both republishing it.

    Never raises: it runs on the worker's own timer and once at startup.
    """
    settings = get_settings()
    window = dt.timedelta(seconds=settings.stranded_run_after_seconds)
    queue = get_run_queue()
    try:
        tenant_ids = await list_runnable_tenant_ids()
    except Exception:
        logger.exception("stranded-run sweep skipped: tenants could not be listed")
        return []

    recovered: list[uuid.UUID] = []
    for tenant_id in tenant_ids:
        try:
            async with tenant_session(tenant_id) as db:
                other = aliased(m.AgentRun)
                candidates = (
                    (
                        await db.execute(
                            select(m.AgentRun)
                            .outerjoin(m.Agent, m.Agent.id == m.AgentRun.agent_id)
                            .where(
                                m.AgentRun.state == RunState.QUEUED.value,
                                m.AgentRun.updated_at < func.now() - window,
                                # A run whose agent is gone is not held:
                                # published, the worker fails it with a reason
                                # instead of it waiting for ever.
                                or_(
                                    m.Agent.id.is_(None),
                                    m.Agent.status.notin_(HELD_AGENT_STATUSES),
                                ),
                                ~exists().where(
                                    other.agent_id == m.AgentRun.agent_id,
                                    other.state == RunState.RUNNING.value,
                                ),
                            )
                            .order_by(m.AgentRun.updated_at)
                            .limit(settings.stranded_run_batch_size)
                            # Only the run row: the worker takes the agent's run
                            # slot THEN the run row, so taking the agent here too
                            # would only add a lock nobody needs.
                            .with_for_update(of=m.AgentRun, skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not candidates:
                    continue
                # Only now ask the stream, and only about these runs. Raises if
                # Redis is unreachable, which rolls the markers back -- there
                # is nothing to publish to either.
                waiting = await queue.undelivered_among({str(run.id) for run in candidates})
                await db.execute(
                    update(m.AgentRun)
                    .where(m.AgentRun.id.in_([run.id for run in candidates]))
                    .values(updated_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                ids = [run.id for run in candidates if str(run.id) not in waiting]
            # Committed, THEN published -- the same order as every run source.
            # A publish that fails here leaves the marker moved, so the run is
            # tried again one window later rather than on every tick.
        except Exception:
            logger.exception("stranded-run sweep failed for tenant %s", tenant_id)
            continue
        for run_id in ids:
            try:
                await queue.enqueue(run_id=run_id, tenant_id=tenant_id)
            except Exception:
                logger.exception("stranded run %s could not be republished", run_id)
                continue
            logger.warning(
                "recovered stranded run %s (tenant %s): it was queued with no "
                "stream entry; republished",
                run_id,
                tenant_id,
            )
            recovered.append(run_id)
    return recovered


#: Run states during which an agent may rightly show "running" without a
#: committed RUNNING run: a run parked on a question or an approval leaves it so.
_HOLDS_RUNNING = (
    RunState.RUNNING.value,
    RunState.WAITING_FOR_INPUT.value,
    RunState.WAITING_FOR_APPROVAL.value,
)

#: How long an agent must have shown "running" unchanged before the sweep looks
#: at it. The run slot is what proves nobody is executing it; this only keeps
#: the sweep away from a status that was written a moment ago.
STALE_RUNNING_AFTER = dt.timedelta(seconds=2 * HEARTBEAT_SECONDS)


async def _holds_running(db: AsyncSession, agent_id: uuid.UUID) -> bool:
    """Whether a run of `agent_id` is RUNNING or parked, as committed now."""
    return bool(
        (
            await db.execute(
                select(
                    exists().where(
                        m.AgentRun.agent_id == agent_id,
                        m.AgentRun.state.in_(_HOLDS_RUNNING),
                    )
                )
            )
        ).scalar_one()
    )


async def release_stale_running_agents(*, now: dt.datetime | None = None) -> list[uuid.UUID]:
    """Set back to idle every agent shown "running" that nothing is running.

    The executor commits "running" in a transaction of its own
    (`oc8.runtime.agent_status`), so a process that dies before the run's own
    commit leaves the agent "running" while its run is `queued` again -- and if
    the redelivered run then ends on an exit that leaves the status alone (a
    paused agent's cancel drain), nothing ever corrects it.

    An agent is left alone while any of these holds:

    * a run of it is RUNNING or parked (`_HOLDS_RUNNING`), as committed;
    * its run slot is taken -- an in-process run holds it for the whole run,
      whose RUNNING transition is not committed until it ends, so this is the
      only sign of it another session can see. Tried, never waited for;
    * its status changed within `STALE_RUNNING_AFTER`;
    * it is paused (`pause_reason`), or its row is locked (SKIP LOCKED).

    Never raises: it runs on the worker's own timer."""
    from oc8.runtime.agent_status import RUNNING, slot_key

    cutoff = (now or dt.datetime.now(tz=dt.UTC)) - STALE_RUNNING_AFTER
    released: list[uuid.UUID] = []
    for tenant_id in await list_active_tenant_ids():
        try:
            async with tenant_session(tenant_id) as db:
                candidates = (
                    (
                        await db.execute(
                            select(m.Agent)
                            .where(
                                m.Agent.status == RUNNING,
                                m.Agent.pause_reason.is_(None),
                                m.Agent.updated_at < cutoff,
                                ~exists().where(
                                    m.AgentRun.agent_id == m.Agent.id,
                                    m.AgentRun.state.in_(_HOLDS_RUNNING),
                                ),
                            )
                            # The row first, then the slot -- and neither waited
                            # for, so this sweep never queues behind a run.
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for agent in candidates:
                    free = (
                        await db.execute(
                            select(
                                func.pg_try_advisory_xact_lock(
                                    func.hashtextextended(slot_key(agent.id), 0)
                                )
                            )
                        )
                    ).scalar_one()
                    if not free:
                        continue
                    # Asked again, now that the slot is ours: an isolated run
                    # commits RUNNING and releases the slot before its container
                    # starts, and may have done both since the select above.
                    # A fresh statement sees that commit; the slot keeps the
                    # next one out until this transaction ends.
                    if await _holds_running(db, agent.id):
                        continue
                    agent.status = "idle"
                    publish_after_commit(db, partial(publish_agent_status, agent))
                    released.append(agent.id)
        except Exception:
            logger.exception("reconciler: running-agent sweep failed for tenant %s", tenant_id)
    if released:
        logger.warning(
            "reconciler: %d agent(s) showed running with nothing running: %s",
            len(released),
            ", ".join(str(a) for a in released),
        )
    return released


__all__ = [
    "ABANDONED_AFTER",
    "HEARTBEAT_SECONDS",
    "HELD_AGENT_STATUSES",
    "REASON",
    "STALE_RUNNING_AFTER",
    "close_abandoned_runs",
    "heartbeat",
    "recover_stranded_runs",
    "release_stale_running_agents",
]


@contextlib.asynccontextmanager
async def heartbeat(
    *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> AsyncIterator[None]:
    """Report that this run is still being executed, for as long as it is.

    Wrapped around the runtime call rather than left to each adapter: there are
    three of them, they run in different processes, and a liveness signal that
    one of them forgets is worse than none -- it would read as a death.

    Its own session per beat. The executor's session is handed to the runtime,
    which commits inside it, and two writers on one session is how a deadlock
    starts.
    """

    async def _beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with tenant_session(tenant_id) as db:
                    await db.execute(
                        update(m.AgentRun)
                        .where(m.AgentRun.id == run_id)
                        .values(updated_at=dt.datetime.now(tz=dt.UTC))
                    )
                    await db.commit()
            except Exception:
                # One missed beat is not a death: the threshold is twenty of
                # them. Logging every hiccup at error level would drown the log
                # a real failure has to be found in.
                logger.debug("heartbeat missed for run %s", run_id, exc_info=True)

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
