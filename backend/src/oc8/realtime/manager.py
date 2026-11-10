"""Holds per-tenant WebSocket sets and one Redis Pub/Sub subscriber task per
tenant. A message on a tenant's channel is fanned out ONLY to that tenant's
sockets -- the security-critical isolation invariant."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

import redis.asyncio as redis

from oc8.realtime.bus import channel_for

logger = logging.getLogger(__name__)

# A subscriber that dies on a transient Redis error resubscribes so its tenant
# doesn't go permanently silent; the backoff (seconds) keeps a persistently-down
# Redis from turning that into a busy loop.
_RESUBSCRIBE_BACKOFF_BASE = 1.0
_RESUBSCRIBE_BACKOFF_MAX = 30.0


#: How long a run watch is trusted before it is asked again. A watch is a grant
#: made at one moment; a member whose seat is revoked, whose role changes or who
#: is offboarded must stop receiving the run within this window.
WATCH_RECHECK_SECONDS = 60.0
#: After a re-check FAILS (a database blip), the watch's frames are withheld
#: and the re-check is not asked again until this much later -- never per frame.
WATCH_RETRY_SECONDS = 15.0
#: While a re-check of a watch that was valid until now is in flight, its frames
#: keep flowing for at most this long (manager clock); then they are withheld.
WATCH_RECHECK_GRACE_SECONDS = 5.0
#: A re-check that has not answered after this long (wall clock) counts as failed.
WATCH_RECHECK_TIMEOUT_SECONDS = 5.0

#: A re-check's answer: still allowed; lost access to the run (the watch is
#: dropped and the client told `run.unwatched`); or the member is gone (the
#: socket is closed).
WatchVerdict = Literal["ok", "access", "gone"]
Recheck = Callable[[str], Awaitable[WatchVerdict]]


class SupportsSend(Protocol):
    async def send_text(self, data: str) -> None: ...


@dataclass(eq=False)
class _Watch:
    #: When the watch was last (re)authorised, on the manager's clock.
    checked_at: float
    #: Set while a re-check is in flight (when it started).
    pending_since: float | None = None
    #: Set after a re-check failed: frames are withheld until then.
    retry_at: float | None = None


@dataclass(eq=False)
class _Conn:
    ws: SupportsSend
    member_id: str | None
    feed: bool
    runs: dict[str, _Watch] = field(default_factory=dict)
    recheck: Recheck | None = None


def _route(conn: _Conn, raw: str) -> tuple[str | None, str | None]:
    """`(frame, watched_run_id)`: the frame for this socket (or None), and the
    run id when it is delivered ONLY because of a run watch -- the one case the
    caller has to re-authorise from time to time."""
    try:
        env = json.loads(raw)
    except ValueError:
        return (raw if conn.feed else None), None
    if not isinstance(env, dict):
        return (raw if conn.feed else None), None
    if "audience" in env:
        audience = env["audience"]
        if not isinstance(audience, list) or conn.member_id is None:
            return None, None
        if conn.member_id not in audience:
            return None, None
        trimmed = {k: v for k, v in env.items() if k != "audience"}
        return json.dumps(trimmed), None
    if conn.feed:
        return raw, None
    type_ = str(env.get("type", ""))
    data = env.get("data")
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if type_.startswith("run.") and isinstance(run_id, str) and run_id in conn.runs:
        return raw, run_id
    return None, None


def _deliverable(conn: _Conn, raw: str) -> str | None:
    """What this socket receives for one channel message, or None.

    The isolation invariant per TENANT is the channel; this is the second one,
    per MEMBER:

    1. an envelope with an `audience` goes to those members' sockets only --
       feed sockets outside it included -- with the `audience` key stripped;
    2. otherwise a feed socket (`run:view`) gets everything, as before;
    3. a member-only socket gets nothing but `run.*` events of runs it was
       allowed to watch (`ws.py`'s `run.watch`).

    Unparseable frames are only ever sent to feed sockets (the legacy
    behaviour), never to member-only sockets. A watch-delivered frame is
    additionally subject to `ConnectionManager`'s periodic re-check."""
    return _route(conn, raw)[0]


class ConnectionManager:
    def __init__(
        self,
        url: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        watch_recheck_seconds: float = WATCH_RECHECK_SECONDS,
        watch_retry_seconds: float = WATCH_RETRY_SECONDS,
        watch_recheck_grace_seconds: float = WATCH_RECHECK_GRACE_SECONDS,
        watch_recheck_timeout_seconds: float = WATCH_RECHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._client: redis.Redis = redis.from_url(url, decode_responses=True)
        self._clock = clock
        self._watch_recheck_seconds = watch_recheck_seconds
        self._watch_retry_seconds = watch_retry_seconds
        self._watch_recheck_grace_seconds = watch_recheck_grace_seconds
        self._watch_recheck_timeout_seconds = watch_recheck_timeout_seconds
        self._rechecks: set[asyncio.Task[None]] = set()
        self._conns: dict[uuid.UUID, dict[int, _Conn]] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def connect(
        self,
        tenant_id: uuid.UUID,
        ws: SupportsSend,
        *,
        member_id: uuid.UUID | None = None,
        feed: bool = True,
        recheck: Recheck | None = None,
    ) -> None:
        """`recheck` re-authorises this socket's run watches (see
        `_still_watching`); `ws.py` always passes one for a member socket."""
        first = tenant_id not in self._conns
        self._conns.setdefault(tenant_id, {})[id(ws)] = _Conn(
            ws=ws,
            member_id=str(member_id) if member_id else None,
            feed=feed,
            recheck=recheck,
        )
        if first:
            ready = asyncio.Event()
            self._tasks[tenant_id] = asyncio.create_task(self._run_subscriber(tenant_id, ready))
            # Only return once the subscription is actually live, so the WS
            # handshake ("realtime.connected") is accurate and no event
            # published right after connect() can be missed. A second
            # connection to an already-subscribed tenant skips this entirely
            # (the subscription is already live).
            try:
                await asyncio.wait_for(ready.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("subscription for tenant %s not confirmed live within 5s", tenant_id)

    async def disconnect(self, tenant_id: uuid.UUID, ws: SupportsSend) -> None:
        conns = self._conns.get(tenant_id)
        if conns is None:
            return
        conns.pop(id(ws), None)
        if not conns:
            self._conns.pop(tenant_id, None)
            task = self._tasks.pop(tenant_id, None)
            if task is not None:
                task.cancel()

    def subscriber_count(self) -> int:
        return len(self._tasks)

    def watch_run(self, tenant_id: uuid.UUID, ws: SupportsSend, run_id: str) -> None:
        """Let this socket receive `run.*` events of `run_id`. The caller has
        already decided it may (`oc8.chat.watch.may_watch_run`)."""
        conn = self._conns.get(tenant_id, {}).get(id(ws))
        if conn is not None:
            conn.runs[run_id] = _Watch(checked_at=self._clock())

    def _still_watching(self, tenant_id: uuid.UUID, conn: _Conn, run_id: str) -> bool:
        """Whether a watch-delivered frame may go out. Never blocks: the
        socket's `recheck` (the same predicate `run.watch` was granted with) is
        started as a background task at most once per `watch_recheck_seconds`
        per watch, and its answer is applied when it lands (`_recheck`).

        While a re-check of a still-valid watch is in flight, frames keep going
        out for at most the grace window, then are withheld. A failed re-check
        withholds the watch's frames until `retry_at` without asking again, so a
        database blip cannot turn into a query per streamed token."""
        watch = conn.runs.get(run_id)
        if watch is None:
            return False
        if conn.recheck is None:
            return True
        now = self._clock()
        if watch.pending_since is not None:
            in_grace = now - watch.pending_since < self._watch_recheck_grace_seconds
            return watch.retry_at is None and in_grace
        if watch.retry_at is not None:
            if now >= watch.retry_at:
                self._start_recheck(tenant_id, conn, run_id, watch, now)
            return False
        if now - watch.checked_at < self._watch_recheck_seconds:
            return True
        self._start_recheck(tenant_id, conn, run_id, watch, now)
        return True

    def _start_recheck(
        self, tenant_id: uuid.UUID, conn: _Conn, run_id: str, watch: _Watch, now: float
    ) -> None:
        watch.pending_since = now
        task = asyncio.create_task(self._recheck(tenant_id, conn, run_id, watch))
        self._rechecks.add(task)
        task.add_done_callback(self._rechecks.discard)

    async def _recheck(self, tenant_id: uuid.UUID, conn: _Conn, run_id: str, watch: _Watch) -> None:
        """Apply one re-check's answer. "ok" renews the watch; "access" drops
        it and tells the client `run.unwatched`; "gone" closes the socket. A
        failure or timeout sets the retry time."""
        recheck = conn.recheck
        if recheck is None:
            return
        try:
            verdict = await asyncio.wait_for(
                recheck(run_id), timeout=self._watch_recheck_timeout_seconds
            )
        except Exception:
            logger.warning("run watch re-check failed (run=%s)", run_id, exc_info=True)
            watch.pending_since = None
            watch.retry_at = self._clock() + self._watch_retry_seconds
            return
        if conn.runs.get(run_id) is not watch:
            return  # re-granted or dropped meanwhile; this answer is stale
        watch.pending_since = None
        if verdict == "ok":
            watch.checked_at = self._clock()
            watch.retry_at = None
            return
        conn.runs.pop(run_id, None)
        if verdict == "gone":
            await self._close(tenant_id, conn)
            return
        try:
            await conn.ws.send_text(
                json.dumps({"type": "run.unwatched", "runId": run_id, "reason": "access"})
            )
        except Exception:
            await self.disconnect(tenant_id, conn.ws)

    async def _settle_rechecks(self) -> None:
        """Wait for every in-flight re-check (tests drive the clock with it)."""
        while self._rechecks:
            await asyncio.gather(*list(self._rechecks), return_exceptions=True)

    async def _close(self, tenant_id: uuid.UUID, conn: _Conn) -> None:
        """Close a member socket whose member no longer exists, then forget it.
        Closed first: forgetting the tenant's last socket cancels the very
        subscriber task this runs in, and the close must not be cut short."""
        close = getattr(conn.ws, "close", None)
        if close is not None:
            try:
                await close(code=1008)
            except Exception:
                logger.debug("closing a revoked member socket failed", exc_info=True)
        await self.disconnect(tenant_id, conn.ws)

    async def _broadcast(self, tenant_id: uuid.UUID, text: str) -> None:
        dead: list[SupportsSend] = []
        for conn in list(self._conns.get(tenant_id, {}).values()):
            frame, watched_run = _route(conn, text)
            if frame is None:
                continue
            if watched_run is not None and not self._still_watching(tenant_id, conn, watched_run):
                continue
            try:
                await conn.ws.send_text(frame)
            except Exception:
                dead.append(conn.ws)
        for ws in dead:
            await self.disconnect(tenant_id, ws)

    async def _run_subscriber(
        self, tenant_id: uuid.UUID, ready: asyncio.Event | None = None
    ) -> None:
        # Resubscribe on a transient failure so the tenant self-heals instead of
        # going permanently silent (the previous version returned on the first
        # error, leaving _tasks stale and every future connect for that tenant
        # without a subscriber). The loop ends only when the tenant has no
        # sockets left or the task is cancelled (last disconnect / close).
        backoff = _RESUBSCRIBE_BACKOFF_BASE
        while tenant_id in self._conns:
            pubsub = self._client.pubsub()
            try:
                await pubsub.subscribe(channel_for(tenant_id))
                if ready is not None:
                    # Subscription is live; unblock connect(). Only the first
                    # successful subscribe signals it.
                    ready.set()
                    ready = None
                backoff = _RESUBSCRIBE_BACKOFF_BASE  # healthy again
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    await self._broadcast(tenant_id, str(message["data"]))
            except asyncio.CancelledError:
                await pubsub.aclose()  # type: ignore[no-untyped-call]
                raise
            except Exception:
                logger.warning(
                    "subscriber failed for tenant %s; resubscribing", tenant_id, exc_info=True
                )
                await pubsub.aclose()  # type: ignore[no-untyped-call]
                if tenant_id in self._conns:
                    await asyncio.sleep(min(backoff, _RESUBSCRIBE_BACKOFF_MAX))
                    backoff *= 2
                continue
            else:
                # listen() ended without error (unusual for pubsub); re-check
                # whether the tenant still has sockets and resubscribe if so.
                await pubsub.aclose()  # type: ignore[no-untyped-call]
        # No sockets left -> drop any stale task handle for this tenant.
        self._tasks.pop(tenant_id, None)

    async def close(self) -> None:
        for recheck in list(self._rechecks):
            recheck.cancel()
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._conns.clear()
        await self._client.aclose()
