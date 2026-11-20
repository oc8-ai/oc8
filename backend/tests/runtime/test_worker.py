from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
import redis.asyncio as redis
from redis import exceptions as redis_exceptions

from oc8.runtime import reconcile
from oc8.runtime.queue import RunMessage, RunQueue
from oc8.runtime.worker import LEASE_LOST_AFTER_MS, run_worker

pytestmark = pytest.mark.asyncio

#: Scaled stand-in for LEASE_LOST_AFTER_MS in the tests below: with the renewal
#: beat monkeypatched to 50ms, 500ms is ten missed renewals -- the same shape as
#: 30s beats against a 10-minute window, at a length a test can wait out.
_WINDOW_MS = 500
_BEAT = 0.05


async def test_worker_handles_one_message(redis_url: str) -> None:
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    seen: list[RunMessage] = []

    async def handler(msg: RunMessage) -> None:
        seen.append(msg)

    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    try:
        await q.enqueue(run_id=run_id, tenant_id=tenant_id)
        await run_worker(q, once=True, handler=handler)
    finally:
        await q.close()

    assert len(seen) == 1
    assert seen[0]["run_id"] == str(run_id)


async def test_worker_isolates_handler_faults(redis_url: str) -> None:
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")

    async def raising_handler(msg: RunMessage) -> None:
        raise RuntimeError("boom")

    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    try:
        await q.enqueue(run_id=run_id, tenant_id=tenant_id)
        # Must not raise: a bad message is logged and swallowed, not propagated.
        await run_worker(q, once=True, handler=raising_handler)
    finally:
        await q.close()


async def test_worker_acks_processed_message(redis_url: str) -> None:
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")

    async def handler(msg: RunMessage) -> None:
        return None

    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    try:
        await q.enqueue(run_id=run_id, tenant_id=tenant_id)
        await run_worker(q, once=True, handler=handler)
        # A successfully processed message is acked: nothing left pending.
        assert await q.claim_stale(min_idle_ms=0) == []
    finally:
        await q.close()


async def test_worker_does_not_ack_when_handler_raises(redis_url: str) -> None:
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")

    async def raising_handler(msg: RunMessage) -> None:
        raise RuntimeError("boom")

    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    try:
        await q.enqueue(run_id=run_id, tenant_id=tenant_id)
        await run_worker(q, once=True, handler=raising_handler)
        # The handler raised, so the entry was never acked: it stays pending
        # and will be reclaimed/retried after the idle window.
        pending = await q.claim_stale(min_idle_ms=0)
        assert len(pending) == 1
        assert pending[0]["run_id"] == str(run_id)
    finally:
        await q.close()


async def test_worker_drains_stale_before_fresh(redis_url: str) -> None:
    key = f"test:{uuid.uuid4()}"
    q = RunQueue(key=key, url=redis_url)
    # A second consumer in the same group that dequeues without acking, leaving
    # a pending entry to be reclaimed by the first worker on its next loop.
    stuck = RunQueue(key=key, url=redis_url, consumer="stuck")

    to_handler: list[RunMessage] = []
    to_recovery: list[RunMessage] = []

    async def handler(msg: RunMessage) -> None:
        to_handler.append(msg)

    async def recovery(msg: RunMessage) -> None:
        to_recovery.append(msg)

    raw: redis.Redis = redis.from_url(redis_url, decode_responses=True)
    stale_run, fresh_run = uuid.uuid4(), uuid.uuid4()
    tenant_id = uuid.uuid4()
    try:
        # First message becomes the stale/reclaimed one; `stuck` claims it into
        # the group's PEL but never acks.
        await q.enqueue(run_id=stale_run, tenant_id=tenant_id)
        claimed = await stuck.dequeue(timeout=5.0)
        assert claimed is not None
        # Backdate its idle time past the worker's reclaim window, simulating a
        # worker that died an hour ago and has renewed nothing since. XCLAIM
        # keeps `stuck` as owner (still unacked) but sets idle to an hour. (It
        # used to be exactly 10 minutes, which is now the window itself -- the
        # assertion is unchanged, the fixture just has to stay clear of the
        # boundary it is meant to be far past.)
        await raw.xclaim(key, "workers", "stuck", 0, [claimed["entry_id"]], idle=3_600_000)
        # A second, fresh message the worker will read via dequeue this loop.
        await q.enqueue(run_id=fresh_run, tenant_id=tenant_id)

        # One iteration: drain stale first (reclaimed -> recovery, redelivered),
        # then dequeue the fresh one (-> handler).
        await run_worker(q, once=True, handler=handler, recovery=recovery)

        assert len(to_recovery) == 1
        assert to_recovery[0]["run_id"] == str(stale_run)
        assert to_recovery[0]["redelivered"] is True
        assert len(to_handler) == 1
        assert to_handler[0]["run_id"] == str(fresh_run)
        assert to_handler[0]["redelivered"] is False
    finally:
        await raw.aclose()
        await stuck.close()
        await q.close()


async def test_housekeeping_runs_before_the_worker_takes_work(redis_url: str) -> None:
    """The sweep for abandoned runs lives HERE and not in the scheduler, because
    the worker is the only process with the docker socket -- and giving a second
    one root-equivalent access to answer a bookkeeping question would be a poor
    trade. On a timer rather than only at startup: a worker may run for weeks,
    and a run abandoned in hour two would otherwise stay open until the next
    deploy."""
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    swept = 0

    async def housekeeping() -> None:
        nonlocal swept
        swept += 1

    try:
        await run_worker(q, once=True, handler=_noop, housekeeping=housekeeping)
    finally:
        await q.close()

    assert swept == 1


async def test_a_failing_sweep_does_not_stop_the_worker(redis_url: str) -> None:
    """Housekeeping is never the reason work stops being picked up."""
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    seen: list[RunMessage] = []

    async def handler(msg: RunMessage) -> None:
        seen.append(msg)

    async def housekeeping() -> None:
        raise RuntimeError("docker went away")

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=handler, housekeeping=housekeeping)
    finally:
        await q.close()

    assert len(seen) == 1


async def test_without_a_hook_nothing_changes(redis_url: str) -> None:
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    seen: list[RunMessage] = []

    async def handler(msg: RunMessage) -> None:
        seen.append(msg)

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=handler)
    finally:
        await q.close()

    assert len(seen) == 1


async def _noop(msg: RunMessage) -> None:
    return None


async def test_the_queue_never_declares_death_sooner_than_the_reconciler() -> None:
    """The three clocks, reduced to one story. They used to disagree: the queue
    reclaimed at 5 minutes while the database heartbeat allowed 10, so every run
    between the two was failed as "lease lost" by the other worker while it was
    still working (run 019fc303, 2026-08-02, 00:05:03, container still Up).

    Both assertions are about a future edit, not about today's arithmetic: the
    queue must never be the impatient one, and the window must stay many beats
    long -- a window a couple of missed renewals can exhaust is this defect
    again, with a shorter fuse."""
    assert LEASE_LOST_AFTER_MS >= reconcile.ABANDONED_AFTER.total_seconds() * 1000
    assert LEASE_LOST_AFTER_MS >= 10 * reconcile.HEARTBEAT_SECONDS * 1000


async def test_a_run_that_outlives_the_window_keeps_its_claim(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE DEFECT. The handler works for twice the reclaim window, and asks a
    second worker -- from inside the handler, while it is still running -- to try
    to take the entry. It must get nothing: a slow run is not a dead one, and
    liveness now reaches the place that decides.

    Driven by a shortened window rather than by sleeping five minutes; the ratio
    of beat to window is the same as production's 30s against 10 minutes."""
    monkeypatch.setattr(reconcile, "HEARTBEAT_SECONDS", _BEAT)
    key = f"test:{uuid.uuid4()}"
    q = RunQueue(redis_url, key=key, consumer="worker-1")
    rival = RunQueue(redis_url, key=key, consumer="worker-2")
    stolen: list[RunMessage] = []

    async def slow_handler(msg: RunMessage) -> None:
        await asyncio.sleep(_WINDOW_MS * 2 / 1000)
        stolen.extend(await rival.claim_stale(min_idle_ms=_WINDOW_MS))

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=slow_handler)

        assert stolen == []
        # And it finished the ordinary way: acked, nothing left pending.
        assert await q.claim_stale(min_idle_ms=0) == []
    finally:
        await rival.close()
        await q.close()


async def test_a_worker_that_dies_mid_run_loses_its_entry(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety net has to survive the fix. "Died" here is the real thing --
    the task driving the loop stops, so the renewal stops with it: no ack, no
    further touches. A second worker takes the entry back, exactly as before."""
    monkeypatch.setattr(reconcile, "HEARTBEAT_SECONDS", _BEAT)
    key = f"test:{uuid.uuid4()}"
    q = RunQueue(redis_url, key=key, consumer="worker-1")
    rival = RunQueue(redis_url, key=key, consumer="worker-2")
    run_id = uuid.uuid4()
    started = asyncio.Event()

    async def never_returns(msg: RunMessage) -> None:
        started.set()
        await asyncio.sleep(3600)

    try:
        await q.enqueue(run_id=run_id, tenant_id=uuid.uuid4())
        worker = asyncio.create_task(run_worker(q, once=True, handler=never_returns))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        await asyncio.sleep(_BEAT * 4)  # several renewals land first

        worker.cancel()  # <- the death
        with contextlib.suppress(asyncio.CancelledError):
            await worker

        await asyncio.sleep(_WINDOW_MS * 2 / 1000)  # silence, past the window
        reclaimed = await rival.claim_stale(min_idle_ms=_WINDOW_MS)
        assert [r["run_id"] for r in reclaimed] == [str(run_id)]
        assert reclaimed[0]["redelivered"] is True
    finally:
        await rival.close()
        await q.close()


async def test_an_entry_left_pending_starts_going_stale_at_once(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The renewal must stop the moment the arm does. A renewal task that
    outlived its handler would hold a deferred or failed entry alive for ever --
    the exact opposite defect, and one nothing else in the system would catch."""
    from oc8.runtime.executor import RunDeferred

    monkeypatch.setattr(reconcile, "HEARTBEAT_SECONDS", _BEAT)
    key = f"test:{uuid.uuid4()}"
    q = RunQueue(redis_url, key=key, consumer="worker-1")
    rival = RunQueue(redis_url, key=key, consumer="worker-2")
    run_id = uuid.uuid4()

    async def deferring(msg: RunMessage) -> None:
        raise RunDeferred(msg["run_id"])

    try:
        await q.enqueue(run_id=run_id, tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=deferring)

        await asyncio.sleep(_WINDOW_MS * 2 / 1000)
        reclaimed = await rival.claim_stale(min_idle_ms=_WINDOW_MS)
        assert [r["run_id"] for r in reclaimed] == [str(run_id)]
    finally:
        await rival.close()
        await q.close()


async def test_a_deferred_run_stays_pending(redis_url: str) -> None:
    """One run per agent at a time. The second is DEFERRED, not failed: the work
    is real and still wanted, it is merely not this agent's turn. Leaving the
    entry unacked redelivers it after the idle window, which is a retry with
    backoff for free."""
    from oc8.runtime.executor import RunDeferred

    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")

    async def deferring(msg: RunMessage) -> None:
        raise RunDeferred(msg["run_id"])

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=deferring)
        # Still in the pending list, so a later pass gets it back.
        reclaimed = await q.claim_stale(min_idle_ms=0)
    finally:
        await q.close()

    assert len(reclaimed) == 1


async def test_a_dead_workers_entry_is_reclaimed_before_the_sweep_is_asked(
    redis_url: str,
) -> None:
    """Two deciders, one window, so the order in the loop decides which one gets
    to say what happened.

    The queue's reclaim and the reconciler's sweep now share `ABANDONED_AFTER`,
    which means on a genuinely dead worker both are ready within a beat of each
    other -- where the old five-minute reclaim beat the ten-minute sweep by
    construction. Housekeeping used to run first, so the sweep would close the
    run with its generic "stopped reporting in" and the reclaim would then find
    a FAILED run and return, and "lease lost: worker died mid-run" -- the one
    sentence that names what actually happened -- became unreachable in
    production. Correctness does not rest on this order (both closers take the
    row lock); the diagnosis does.
    """
    key = f"test:{uuid.uuid4()}"
    q = RunQueue(redis_url, key=key, consumer="worker-1")
    stuck = RunQueue(redis_url, key=key, consumer="stuck")
    raw: redis.Redis = redis.from_url(redis_url, decode_responses=True)
    order: list[str] = []

    async def _sweep() -> None:
        order.append("sweep")

    async def _recovery(msg: RunMessage) -> None:
        order.append("reclaim")

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        claimed = await stuck.dequeue(timeout=5.0)
        assert claimed is not None
        # A worker that died an hour ago: no ack, and no renewal since.
        await raw.xclaim(key, "workers", "stuck", 0, [claimed["entry_id"]], idle=3_600_000)

        await run_worker(q, once=True, handler=_noop, recovery=_recovery, housekeeping=_sweep)

        assert order == ["reclaim", "sweep"]
    finally:
        await raw.aclose()
        await stuck.close()
        await q.close()


async def test_a_lost_lease_is_reported_with_both_of_its_causes(
    redis_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A log line that names one cause of a two-cause signal sends an operator
    after a second worker that may not exist.

    `touch` returns False for a rival's ack AND for a pending entry whose stream
    entry was trimmed away (test_queue pins that Redis really does destroy it).
    The worker cannot tell them apart -- so it must not pretend to, least of all
    in the minutes when someone is reading the log to find out what happened.
    """
    import logging

    monkeypatch.setattr(reconcile, "HEARTBEAT_SECONDS", _BEAT)
    key = f"test:{uuid.uuid4()}"
    q = RunQueue(redis_url, key=key, consumer="worker-1")

    async def loses_its_entry(msg: RunMessage) -> None:
        await q.ack(msg["entry_id"])  # stands in for a rival's reclaim + ack
        await asyncio.sleep(_BEAT * 4)

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        with caplog.at_level(logging.WARNING, logger="oc8.runtime.worker"):
            await run_worker(q, once=True, handler=loses_its_entry)

        said = "\n".join(r.getMessage() for r in caplog.records)
        assert "no longer pending" in said
        assert "trimmed" in said, "the cause that is not another worker"
    finally:
        await q.close()


class _FlakyQueue(RunQueue):
    """A queue whose next claim_stale / dequeue / ack fails to reach Redis once."""

    def __init__(self, url: str, *, fail: set[str]) -> None:
        super().__init__(url, key=f"test:{uuid.uuid4()}")
        self.fail = set(fail)

    def _blip(self, name: str) -> None:
        if name in self.fail:
            self.fail.discard(name)
            raise redis_exceptions.TimeoutError("Timeout connecting to server")

    async def claim_stale(self, *, min_idle_ms: int, count: int = 10) -> list[RunMessage]:
        self._blip("claim_stale")
        return await super().claim_stale(min_idle_ms=min_idle_ms, count=count)

    async def dequeue(self, *, timeout: float = 5.0) -> RunMessage | None:  # noqa: ASYNC109
        self._blip("dequeue")
        return await super().dequeue(timeout=timeout)

    async def ack(self, entry_id: str) -> None:
        self._blip("ack")
        await super().ack(entry_id)


async def test_an_unreachable_queue_while_reclaiming_does_not_stop_the_worker(
    redis_url: str,
) -> None:
    """A Redis connect timeout in the reclaim sweep is a blip: the worker goes on
    to take fresh work instead of exiting (and taking its runs with it)."""
    q = _FlakyQueue(redis_url, fail={"claim_stale"})
    seen: list[RunMessage] = []

    async def handler(msg: RunMessage) -> None:
        seen.append(msg)

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=handler)
    finally:
        await q.close()

    assert len(seen) == 1


async def test_an_unreachable_queue_while_dequeuing_does_not_stop_the_worker(
    redis_url: str,
) -> None:
    q = _FlakyQueue(redis_url, fail={"dequeue"})
    seen: list[RunMessage] = []

    async def handler(msg: RunMessage) -> None:
        seen.append(msg)

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=handler)  # the blip: must not raise
        await run_worker(q, once=True, handler=handler)  # the next turn takes it
    finally:
        await q.close()

    assert len(seen) == 1


async def test_a_lost_ack_leaves_the_entry_pending_instead_of_raising(redis_url: str) -> None:
    q = _FlakyQueue(redis_url, fail={"ack"})

    try:
        await q.enqueue(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await run_worker(q, once=True, handler=_noop)
        # Not acked, so it is reclaimed later -- recover_reclaimed reads the row.
        assert len(await q.claim_stale(min_idle_ms=0)) == 1
    finally:
        await q.close()
