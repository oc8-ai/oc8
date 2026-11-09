"""Redis Streams job queue for durable runs.

Backed by a Redis Stream with a consumer group so that a worker crashing
mid-run does not lose the message: unacked entries stay in the group's
pending entries list (PEL) and can be reclaimed via `claim_stale`.

An entry's idle time in the PEL is reset by nothing but XACK and XCLAIM -- it
measures silence from its consumer, not the age of the work. A consumer still
working an entry therefore has to say so with `touch`; how often, and how much
silence means death, is the worker's policy, not the queue's.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from typing import Any, TypedDict, cast

import redis.asyncio as redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from oc8.config import get_settings

logger = logging.getLogger(__name__)


class RunMessage(TypedDict):
    run_id: str
    tenant_id: str
    entry_id: str
    redelivered: bool


def _to_message(entry_id: str, fields: dict[str, Any], *, redelivered: bool) -> RunMessage | None:
    """Build a RunMessage from a raw stream entry, or None if malformed.

    A corrupt/incomplete entry must not raise -- one bad message shouldn't
    wedge the worker loop that's iterating over dequeue()/claim_stale().
    """
    run_id = fields.get("run_id")
    tenant_id = fields.get("tenant_id")
    if not isinstance(run_id, str) or not isinstance(tenant_id, str):
        logger.warning("dropping malformed stream entry %s: %r", entry_id, fields)
        return None
    return RunMessage(
        run_id=run_id,
        tenant_id=tenant_id,
        entry_id=entry_id,
        redelivered=redelivered,
    )


class RunQueue:
    def __init__(
        self,
        url: str,
        *,
        key: str = "oc8:runs:stream",
        group: str = "workers",
        consumer: str | None = None,
    ) -> None:
        self._client: redis.Redis = redis.from_url(url, decode_responses=True)
        self._key = key
        self._group = group
        self._consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self._group_ready = False

    async def _ensure_group(self) -> None:
        """Idempotent. id="0" (not "$") is defense-in-depth: it guards against
        a message reaching the stream before this group exists -- e.g. a raw
        producer bypassing RunQueue, or the group being destroyed and
        recreated after entries have already accumulated."""
        if self._group_ready:
            return
        try:
            await self._client.xgroup_create(self._key, self._group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        await self._ensure_group()
        await self._client.xadd(
            self._key,
            {"run_id": str(run_id), "tenant_id": str(tenant_id)},
            maxlen=10_000,
            approximate=True,
        )

    async def undelivered_among(self, run_ids: set[str]) -> set[str]:
        """Which of `run_ids` have an entry in the stream that no consumer of
        this group has been handed yet -- the backlog.

        A queued run in here is waiting for a worker, not stranded, however old
        it is: under load the backlog can be older than any threshold. The
        backlog is scanned in pages (it is bounded by the stream's MAXLEN) and
        only the asked-for ids are kept, so the answer never grows past them.
        An entry without a usable run id is skipped, not fatal: one corrupt
        message must not stop the caller from recovering every other run.
        """
        if not run_ids:
            return set()
        await self._ensure_group()
        groups: list[dict[str, Any]] = await self._client.xinfo_groups(self._key)
        cursor = next(
            (str(g["last-delivered-id"]) for g in groups if g.get("name") == self._group),
            "0-0",
        )
        found: set[str] = set()
        while True:
            # "(" makes the range exclusive: everything after the cursor.
            page = cast(
                list[tuple[str, dict[str, str]]],
                await self._client.xrange(self._key, min=f"({cursor}", max="+", count=500),
            )
            for _entry_id, fields in page:
                run_id = fields.get("run_id") if isinstance(fields, dict) else None
                if isinstance(run_id, str) and run_id in run_ids:
                    found.add(run_id)
            if len(page) < 500 or found == run_ids:
                return found
            cursor = page[-1][0]

    async def dequeue(self, *, timeout: float = 5.0) -> RunMessage | None:  # noqa: ASYNC109
        await self._ensure_group()
        try:
            raw_result = await self._client.xreadgroup(
                self._group,
                self._consumer,
                {self._key: ">"},
                count=1,
                block=int(timeout * 1000),
            )
        except RedisTimeoutError:
            # redis-py's asyncio connection layer conflates a BLOCK-expiry on
            # xreadgroup with a genuine socket read timeout: for some timeout
            # values it raises TimeoutError instead of returning an empty
            # result (see connection.py's read_response). A BLOCK expiry just
            # means "no message arrived in the window" -- exactly what the
            # empty raw_result path below already returns, so treat it the
            # same way. If the connection actually dropped, redis-py
            # transparently reconnects on the next command.
            return None
        if not raw_result:
            return None
        # redis-py's XReadGroupResponse type covers RESP2 and RESP3 wire shapes,
        # but `redis.from_url` here uses the default protocol (RESP2) with
        # decode_responses=True, which always yields
        # list[[stream_key, list[(entry_id, fields)]]] -- confirmed against the
        # installed redis-py/server.
        result = cast(list[tuple[str, list[tuple[str, dict[str, str]]]]], raw_result)
        _stream_key, entries = result[0]
        if not entries:
            return None
        entry_id, fields = entries[0]
        return _to_message(entry_id, fields, redelivered=False)

    async def ack(self, entry_id: str) -> None:
        await self._client.xack(self._key, self._group, entry_id)

    async def touch(self, entry_id: str) -> bool:
        """Renew this consumer's claim on an entry it is still working.

        Returns False when the entry is no longer pending for this group. Three
        causes, and this call cannot tell them apart, so no caller may claim it
        can: it was reclaimed and acked by someone else, it was acked already,
        or the STREAM entry behind it is gone -- Redis 7 has XCLAIM delete a
        pending entry whose stream entry no longer exists, which is what a
        MAXLEN-trimmed stream (see `enqueue`) does to a long-running entry. In
        that last case the pending entry is destroyed by this call and can never
        be redelivered; the run's database heartbeat is then its only decider,
        which is exactly why there is one. True means we hold it and its idle
        clock is back at zero.

        XCLAIM by the entry's CURRENT owner with min-idle-time 0 and JUSTID.
        Verified against the installed redis-py 8.0.1 and Redis 7 rather than
        trusted: idle went 600000ms -> 1ms, `times_delivered` did NOT increase,
        a following XREADGROUP ">" returned nothing (so no second delivery), and
        the same call on an acked or unknown id returned []. min-idle-time 0
        means this also takes an entry BACK from a worker that reclaimed it
        while we were quiet -- which is the right way round: we are the one
        actually doing the work, and the reclaimer's XACK still lands either way.
        """
        claimed = await self._client.xclaim(
            self._key,
            self._group,
            self._consumer,
            min_idle_time=0,
            message_ids=[entry_id],
            justid=True,
        )
        return bool(claimed)

    async def claim_stale(self, *, min_idle_ms: int, count: int = 10) -> list[RunMessage]:
        """Take over entries whose consumer has gone silent for min_idle_ms.

        No default: silence is only evidence of death against a renewal beat,
        and that beat belongs to the worker (see `oc8.runtime.worker`). A default
        here was a third clock nobody reconciled -- it read 5 minutes while the
        run's database heartbeat allowed 10, so every run in between was failed
        as "lease lost" by a second worker while it was still working.
        """
        await self._ensure_group()
        result = await self._client.xautoclaim(
            self._key,
            self._group,
            self._consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        # Redis 7 / redis-py return [next_cursor, entries, deleted_ids]; older
        # clients returned just [next_cursor, entries]. Index defensively
        # rather than assuming a fixed arity.
        entries = result[1]
        messages = []
        for entry_id, fields in entries:
            msg = _to_message(entry_id, fields, redelivered=True)
            if msg is not None:
                messages.append(msg)
        return messages

    async def close(self) -> None:
        await self._client.aclose()


_queue: RunQueue | None = None


def get_run_queue() -> RunQueue:
    global _queue
    if _queue is None:
        _queue = RunQueue(get_settings().redis_url)
    return _queue
