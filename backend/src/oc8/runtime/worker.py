"""Background worker: pop run messages off the queue and execute them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from oc8.edition.runtime import COMMUNITY_RUNTIME_COMPOSITION, EditionRuntimeComposition
from oc8.runtime import reconcile
from oc8.runtime.executor import RunDeferred, execute_run, recover_reclaimed
from oc8.runtime.queue import RunMessage, RunQueue

logger = logging.getLogger(__name__)

Handler = Callable[[RunMessage], Awaitable[None]]

#: Redis could not be reached for one queue call. A blip, not a death: before
#: these were caught, one connect timeout in claim_stale ended run_worker, the
#: process exited, and every run it held -- including one mid-execution in the
#: sibling loop -- waited out the whole lease window before anyone reclaimed it
#: (seen live 2026-11-09 on a loaded Docker Desktop host, three restarts an hour).
_QUEUE_UNREACHABLE = (RedisConnectionError, RedisTimeoutError)

#: Pause after an unreachable queue, so a Redis that is down is not hammered.
_QUEUE_RETRY_SECONDS = 1.0

#: How long a consumer may stop renewing before another worker treats it as dead
#: and reclaims its entry. The SAME window as the reconciler's ABANDONED_AFTER,
#: renewed on the SAME beat as the run's database heartbeat. It replaces a
#: hardcoded 5 minutes in the queue -- see _keep_claimed.
#:
#: One window, two deciders, and they are NOT interchangeable: this one says a
#: consumer stopped talking to Redis, the reconciler's says the work stopped
#: reporting to the database. Neither may kill a run alone, which is why
#: `executor.recover_reclaimed` asks the row before it believes this signal.
LEASE_LOST_AFTER_MS = int(reconcile.ABANDONED_AFTER.total_seconds() * 1000)


@contextlib.asynccontextmanager
async def _keep_claimed(queue: RunQueue, entry_id: str) -> AsyncIterator[None]:
    """Say we are still working this entry, for as long as we are.

    THE DEFECT, measured live 2026-08-02: `heartbeat()` wrote the AgentRun row
    and nothing else, while a stream entry's idle time is reset by nothing but
    XACK/XCLAIM. With two workers up, the other worker's claim_stale reclaimed a
    perfectly live entry after five minutes and failed the run as "lease lost" --
    run 019fc303, dead at 00:05:03 with oc8-nora-agent-019fc303 still Up and
    still working on a result nobody would read. Not a race with two workers: a
    guarantee, for every run longer than the window.

    It lives here rather than in the executor's heartbeat because `_process`
    owns the entry and its ack, and ownership of the claim has to sit with
    ownership of the ack -- threading entry ids down into the executor would put
    two modules in charge of one lease. It also covers every arm (recovery and
    the ingestion handler as well as execute_run) and the whole of the handler,
    where the executor's heartbeat wraps only the runtime call.

    The beat is `reconcile.HEARTBEAT_SECONDS`, read live so the queue's clock and
    the database's stay literally the same clock. The two SPANS are deliberately
    not the same, and it would be wrong to claim they are: this one covers the
    whole arm, the heartbeat only the runtime call, so everything execute_run
    does before and after that call is renewed here and silent there. That is
    survivable only because neither signal is trusted alone -- inside those spans
    the database decider can look at a working run and see silence, and
    `recover_reclaimed` is where the two are made to agree before anything dies.

    WHAT CATCHES A WEDGE -- a handler that is alive and never finishes -- now
    that this renews honestly for as long as the handler runs. Nothing here
    does, and nothing here can: from the queue's side a wedge is indistinguishable
    from work, which is the whole reason the five-minute reclaim had to go. It is
    caught one level down, by each arm's own bound on the WORK, and that is
    per-arm rather than universal:

    * container runtimes: `isolated.DockerIsolatedRuntime` bounds `driver.wait`
      (agent_max_steps * 60) and tears the container down; the nanoclaw plugin
      has its own deadline plus startup and heartbeat watchdogs.
    * the in-process runtime (agent_isolation is on -- container is the
      DEFAULT -- unless a deployment turns it off): every step is bounded,
      the model call by
      modelrouter.streaming's httpx timeout and the tool call by
      `agent.mcp_client.MCP_REQUEST_TIMEOUT_SECONDS`, which existed only after
      this defect was reviewed: before it, one unanswered tool call hung the run,
      the agent and this worker for ever. `agent_max_steps` bounds the count.
    * the INGESTION stream (`knowledge.worker.ingest_job`, run through this same
      function): nothing. Its jobs have no heartbeat and no reconciler sweep, so
      a connector that never returns leaves `IngestionJob.status == 'running'`
      with nothing able to close it. Accepted knowingly: first-party connectors
      bound every fetch (`knowledge.connectors.fetcher`, httpx timeouts), so
      this needs a third-party connector plugin to wedge -- and what the old
      five-minute reclaim did here was not a net either. It failed the job as
      "worker died mid-ingestion" while the sync went on writing chunks, which
      it also did to every honest sync that took longer than five minutes.

    The reclaim below still catches the case it was actually built for: a worker
    that DIES stops renewing, because a dead process renews nothing.
    """

    async def _renew() -> None:
        while True:
            await asyncio.sleep(reconcile.HEARTBEAT_SECONDS)
            try:
                if not await queue.touch(entry_id):
                    # No longer ours to renew. Two causes, and the log must not
                    # pick one: someone reclaimed and acked it, or the stream
                    # entry itself is gone (MAXLEN trimming removes the pending
                    # entry with it -- an operator sent after a second worker
                    # that does not exist wastes the only minutes that matter).
                    # Either way do not stop the work: killing a live run on a
                    # queue signal is the dangerous half, and this worker is the
                    # one actually doing it. The run's terminal transition is
                    # idempotent, and the reclaimer asks the run's heartbeat
                    # before it believes the entry.
                    logger.warning(
                        "entry %s is no longer pending for us (acked elsewhere, or "
                        "trimmed from the stream); still working it here",
                        entry_id,
                    )
                    return
            except Exception:
                # One missed renewal is not a death: the threshold is twenty of
                # them. Same reasoning as the database heartbeat's -- and if
                # Redis stays unreachable for the whole window while the work
                # goes on, the run still survives, because the reclaimer reads
                # the database heartbeat before failing anything.
                logger.debug("claim renewal missed for entry %s", entry_id, exc_info=True)

    task = asyncio.create_task(_renew())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _process(
    queue: RunQueue,
    message: RunMessage,
    arm: Handler,
    runtime_composition: EditionRuntimeComposition,
) -> None:
    """Run one message through its arm; ack on success, leave it pending on
    failure so it's reclaimed and retried after the idle window. Never crashes
    the loop -- a bad message is logged and swallowed, not propagated.

    The claim is renewed for as long as the arm runs, and stops the moment it
    returns or raises -- so an entry left pending deliberately (RunDeferred) or
    by a fault starts going stale immediately, exactly as before.
    """
    try:
        with runtime_composition.activate():
            async with _keep_claimed(queue, message["entry_id"]):
                await arm(message)
    except RunDeferred:
        # Deliberately NOT acked: the message comes back after the idle window,
        # by which time the agent is free. This is the one path where leaving an
        # entry pending is the correct outcome rather than a fault. That retry
        # now waits LEASE_LOST_AFTER_MS rather than the old 5 minutes -- the PEL
        # was always being borrowed as a retry timer, and the window it borrows
        # is the one that says "the executor is dead", which had to move.
        logger.info("run message deferred (run_id=%s)", message.get("run_id"))
        return
    except Exception:
        logger.exception(
            "unhandled error processing run message (run_id=%s)",
            message.get("run_id"),
        )
        return
    try:
        await queue.ack(message["entry_id"])
    except _QUEUE_UNREACHABLE:
        # The work is done; only the ack is lost. The entry goes stale and is
        # reclaimed, and recover_reclaimed reads the run's row before it acts.
        logger.warning("could not ack entry %s; it will be reclaimed", message["entry_id"])


#: How often the worker sweeps for runs nothing is running any more. Cheap when
#: there is nothing to close, and a minute of delay on a run already abandoned
#: for an hour changes nothing.
#:
#: A ceiling, not a promise: the sweep runs at the top of the loop, and the loop
#: is inside `_process` for as long as a run takes -- which since 2026-08-02 is
#: legitimately unbounded. A deployment with as many long runs as workers sweeps
#: nothing until one of them frees up. Left as it is on purpose: the sweep and a
#: run would then be two writers in one process reaching for the same two rows
#: from opposite ends (the executor locks agent then run, the sweep run then
#: agent), and buying an earlier sweep with a deadlock is a poor trade. Nothing
#: is lost by the delay -- an abandoned run stays abandoned, and its worker being
#: busy is proof this host is not the one that died.
_HOUSEKEEPING_SECONDS = 60.0


async def run_worker(
    queue: RunQueue,
    *,
    once: bool = False,
    handler: Handler = execute_run,
    recovery: Handler = recover_reclaimed,
    housekeeping: Callable[[], Awaitable[Any]] | None = None,
    runtime_composition: EditionRuntimeComposition = COMMUNITY_RUNTIME_COMPOSITION,
) -> None:
    # The worker is a separate process from the API server, so it needs its
    # own OTel provider setup -- inert (a no-op) unless otel_enabled, per
    # setup_observability's own contract. Imported lazily so importing this
    # module never pulls in opentelemetry.
    from oc8.config import get_settings
    from oc8.observability import setup_observability, shutdown_observability

    setup_observability(get_settings())
    # Housekeeping lives in the WORKER, not the scheduler, because it is the
    # worker that can see containers -- it is the only process with the docker
    # socket, and giving a second one root-equivalent access to answer a
    # bookkeeping question would be a poor trade. It is also where the container
    # reaper already runs, and this is that sweep's exact inverse.
    loop = asyncio.get_running_loop()
    next_housekeeping = 0.0
    try:
        while True:
            # Drain stale (unacked) entries first -- cheap when the PEL is empty --
            # then block for a fresh message. Reclaimed entries are redelivered and
            # go to `recovery`; fresh ones go to `handler`. Stale means "its
            # consumer stopped renewing", never "it has been running a while":
            # a live worker renews every reconcile.HEARTBEAT_SECONDS.
            #
            # BEFORE housekeeping, and that order is deliberate. The two now
            # share one window (LEASE_LOST_AFTER_MS is ABANDONED_AFTER), so on a
            # genuinely dead worker both fire within a beat of each other, and
            # whichever runs first writes the ending. The reclaim knows more --
            # it can say "lease lost: worker died mid-run" where the sweep can
            # only say "stopped reporting in" -- so it goes first and the sweep
            # finds nothing left to say. (Correctness does not depend on this:
            # both take the run's row lock, so at worst the more specific
            # diagnosis is the one that loses. Only the message is at stake.)
            try:
                stale = await queue.claim_stale(min_idle_ms=LEASE_LOST_AFTER_MS)
            except _QUEUE_UNREACHABLE:
                logger.warning("run queue unreachable while reclaiming; retrying")
                stale = []
            for reclaimed in stale:
                await _process(queue, reclaimed, recovery, runtime_composition)

            if housekeeping is not None and loop.time() >= next_housekeeping:
                next_housekeeping = loop.time() + _HOUSEKEEPING_SECONDS
                try:
                    await housekeeping()
                except Exception:
                    logger.exception("unhandled error in worker housekeeping")

            try:
                message = await queue.dequeue(timeout=5.0)
            except _QUEUE_UNREACHABLE:
                logger.warning("run queue unreachable while dequeuing; retrying")
                await asyncio.sleep(_QUEUE_RETRY_SECONDS)
                message = None
            if message is not None:
                await _process(queue, message, handler, runtime_composition)

            if once:
                return
    finally:
        shutdown_observability()
