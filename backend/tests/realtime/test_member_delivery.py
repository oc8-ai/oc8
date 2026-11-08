from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from oc8.realtime.envelope import build_envelope
from oc8.realtime.manager import ConnectionManager, WatchVerdict

pytestmark = pytest.mark.asyncio


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


def _env(
    tenant: uuid.UUID,
    type_: str,
    data: dict[str, object],
    audience: list[uuid.UUID] | None = None,
) -> str:
    return json.dumps(build_envelope(tenant, type_, data, "test", audience=audience))


async def test_audience_events_reach_only_their_members(redis_url: str) -> None:
    mgr = ConnectionManager(redis_url)
    tenant, alice, bob = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    admin_ws, alice_ws, bob_ws = FakeWS(), FakeWS(), FakeWS()
    await mgr.connect(tenant, admin_ws, member_id=uuid.uuid4(), feed=True)
    await mgr.connect(tenant, alice_ws, member_id=alice, feed=False)
    await mgr.connect(tenant, bob_ws, member_id=bob, feed=False)
    try:
        await mgr._broadcast(
            tenant, _env(tenant, "chat.message_created", {"session_id": "s"}, [alice])
        )
        assert len(alice_ws.sent) == 1
        assert "audience" not in json.loads(alice_ws.sent[0])
        assert bob_ws.sent == []
        # Not tenant-wide: even a feed socket outside the audience gets nothing.
        assert admin_ws.sent == []
    finally:
        await mgr.close()


async def test_member_only_socket_gets_watched_runs_and_nothing_else(redis_url: str) -> None:
    mgr = ConnectionManager(redis_url)
    tenant, alice = uuid.uuid4(), uuid.uuid4()
    ws = FakeWS()
    await mgr.connect(tenant, ws, member_id=alice, feed=False)
    try:
        await mgr._broadcast(tenant, _env(tenant, "activity.logged", {"message": "secret"}))
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "a"}))
        assert ws.sent == []
        mgr.watch_run(tenant, ws, "r1")
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "b"}))
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r2", "text": "c"}))
        assert [json.loads(s)["data"]["text"] for s in ws.sent] == ["b"]
    finally:
        await mgr.close()


async def test_a_member_only_socket_never_gets_an_unparseable_frame(redis_url: str) -> None:
    mgr = ConnectionManager(redis_url)
    tenant = uuid.uuid4()
    member_ws, feed_ws = FakeWS(), FakeWS()
    await mgr.connect(tenant, member_ws, member_id=uuid.uuid4(), feed=False)
    await mgr.connect(tenant, feed_ws)
    try:
        await mgr._broadcast(tenant, "not json")
        assert member_ws.sent == []
        assert feed_ws.sent == ["not json"]
    finally:
        await mgr.close()


async def test_feed_socket_keeps_the_tenant_feed(redis_url: str) -> None:
    mgr = ConnectionManager(redis_url)
    tenant = uuid.uuid4()
    ws = FakeWS()
    await mgr.connect(tenant, ws)  # legacy call: feed=True
    try:
        await mgr._broadcast(tenant, _env(tenant, "activity.logged", {"message": "x"}))
        assert len(ws.sent) == 1
    finally:
        await mgr.close()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ClosableWS(FakeWS):
    def __init__(self) -> None:
        super().__init__()
        self.closed_with: int | None = None

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


def _delta(tenant: uuid.UUID, text: str, run_id: str = "r1") -> str:
    return _env(tenant, "run.token_delta", {"run_id": run_id, "text": text})


def _texts(ws: FakeWS) -> list[str]:
    frames = [json.loads(s) for s in ws.sent]
    return [f["data"]["text"] for f in frames if f.get("type") == "run.token_delta"]


async def test_a_watch_is_rechecked_and_dropped_once_access_is_lost(redis_url: str) -> None:
    """A watch is not a grant for the socket's lifetime: after the re-check
    interval the same predicate runs again (in the background, the frame that
    triggered it still goes out within the grace window), and a member who lost
    the run's agent gets `run.unwatched` and no further events."""
    clock = _Clock()
    mgr = ConnectionManager(redis_url, clock=clock, watch_recheck_seconds=60.0)
    tenant = uuid.uuid4()
    verdict: dict[str, WatchVerdict] = {"value": "ok"}
    checks: list[str] = []

    async def recheck(run_id: str) -> WatchVerdict:
        checks.append(run_id)
        return verdict["value"]

    ws = FakeWS()
    await mgr.connect(tenant, ws, member_id=uuid.uuid4(), feed=False, recheck=recheck)
    try:
        mgr.watch_run(tenant, ws, "r1")
        await mgr._broadcast(tenant, _delta(tenant, "a"))
        clock.now += 30
        await mgr._broadcast(tenant, _delta(tenant, "b"))
        await mgr._settle_rechecks()
        assert checks == [], "re-checked on the hot path"
        clock.now += 31
        await mgr._broadcast(tenant, _delta(tenant, "c"))
        await mgr._settle_rechecks()
        assert checks == ["r1"]
        verdict["value"] = "access"  # her seat is revoked
        clock.now += 61
        await mgr._broadcast(tenant, _delta(tenant, "d"))  # grace: check in flight
        await mgr._settle_rechecks()
        clock.now += 1
        await mgr._broadcast(tenant, _delta(tenant, "e"))
        assert _texts(ws) == ["a", "b", "c", "d"]
        assert json.loads(ws.sent[-1]) == {
            "type": "run.unwatched",
            "runId": "r1",
            "reason": "access",
        }
        await mgr._settle_rechecks()
        assert checks == ["r1", "r1"], "a dropped watch is not re-checked again"
    finally:
        await mgr.close()


async def test_a_member_offboarded_mid_watch_has_the_socket_closed(redis_url: str) -> None:
    clock = _Clock()
    mgr = ConnectionManager(redis_url, clock=clock, watch_recheck_seconds=60.0)
    tenant = uuid.uuid4()

    async def recheck(run_id: str) -> WatchVerdict:
        return "gone"

    ws = ClosableWS()
    await mgr.connect(tenant, ws, member_id=uuid.uuid4(), feed=False, recheck=recheck)
    try:
        mgr.watch_run(tenant, ws, "r1")
        clock.now += 61
        await mgr._broadcast(tenant, _delta(tenant, "x"))
        await mgr._settle_rechecks()
        assert ws.closed_with == 1008
        sent_before = list(ws.sent)
        mgr.watch_run(tenant, ws, "r1")  # the socket is gone from the manager
        await mgr._broadcast(tenant, _delta(tenant, "y"))
        assert ws.sent == sent_before
        assert "y" not in _texts(ws)
    finally:
        await mgr.close()


async def test_a_failing_recheck_backs_off_instead_of_querying_per_frame(
    redis_url: str,
) -> None:
    """Fail closed on a database blip, without a query storm: the watch's
    frames are withheld until the retry time and the re-check is asked once
    per backoff window, not once per frame. Other sockets keep receiving."""
    clock = _Clock()
    mgr = ConnectionManager(
        redis_url, clock=clock, watch_recheck_seconds=60.0, watch_retry_seconds=15.0
    )
    tenant = uuid.uuid4()
    calls = {"n": 0}

    async def recheck(run_id: str) -> WatchVerdict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return "ok"

    ws, other = FakeWS(), FakeWS()
    await mgr.connect(tenant, ws, member_id=uuid.uuid4(), feed=False, recheck=recheck)
    await mgr.connect(tenant, other)
    try:
        mgr.watch_run(tenant, ws, "r1")
        clock.now += 61
        await mgr._broadcast(tenant, _delta(tenant, "a"))  # starts the check
        await mgr._settle_rechecks()  # ... which fails
        for i in range(20):
            clock.now += 0.5
            await mgr._broadcast(tenant, _delta(tenant, f"blip{i}"))
        await mgr._settle_rechecks()
        assert calls["n"] == 1, "re-checked per frame during the backoff"
        clock.now += 6  # past the 15 s retry time
        await mgr._broadcast(tenant, _delta(tenant, "retry"))  # withheld, re-asks
        await mgr._settle_rechecks()
        assert calls["n"] == 2
        await mgr._broadcast(tenant, _delta(tenant, "z"))
        assert [t for t in _texts(ws) if t.startswith("blip") or t == "retry"] == []
        assert _texts(ws)[-1] == "z"
        assert len(_texts(other)) == 23, "the feed socket missed frames"
    finally:
        await mgr.close()


async def test_a_slow_recheck_does_not_stall_the_tenant(redis_url: str) -> None:
    """The re-check runs outside the broadcast loop: a hung database never
    holds up delivery to the tenant's other sockets."""
    clock = _Clock()
    mgr = ConnectionManager(redis_url, clock=clock, watch_recheck_seconds=60.0)
    tenant = uuid.uuid4()
    never = asyncio.Event()

    async def recheck(run_id: str) -> WatchVerdict:
        await never.wait()
        return "ok"

    ws, other = FakeWS(), FakeWS()
    await mgr.connect(tenant, ws, member_id=uuid.uuid4(), feed=False, recheck=recheck)
    await mgr.connect(tenant, other)
    try:
        mgr.watch_run(tenant, ws, "r1")
        clock.now += 61
        await asyncio.wait_for(mgr._broadcast(tenant, _delta(tenant, "a")), timeout=1.0)
        await asyncio.wait_for(mgr._broadcast(tenant, _delta(tenant, "b")), timeout=1.0)
        assert _texts(other) == ["a", "b"]
        clock.now += 10  # past the grace window, the check still unanswered
        await asyncio.wait_for(mgr._broadcast(tenant, _delta(tenant, "c")), timeout=1.0)
        assert _texts(other) == ["a", "b", "c"]
        assert "c" not in _texts(ws)
    finally:
        await mgr.close()
