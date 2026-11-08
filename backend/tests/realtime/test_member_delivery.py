from __future__ import annotations

import json
import uuid

import pytest

from oc8.realtime.envelope import build_envelope
from oc8.realtime.manager import ConnectionManager

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


async def test_a_watch_is_rechecked_and_dropped_once_access_is_lost(redis_url: str) -> None:
    """A watch is not a grant for the socket's lifetime: after the re-check
    interval the same predicate runs again, and a member who lost the run's
    agent gets `run.unwatched` and no further events."""
    clock = _Clock()
    mgr = ConnectionManager(redis_url, clock=clock, watch_recheck_seconds=60.0)
    tenant = uuid.uuid4()
    verdict = {"value": "ok"}
    checks: list[str] = []

    async def recheck(run_id: str) -> str:
        checks.append(run_id)
        return verdict["value"]

    ws = FakeWS()
    await mgr.connect(tenant, ws, member_id=uuid.uuid4(), feed=False, recheck=recheck)
    try:
        mgr.watch_run(tenant, ws, "r1")
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "a"}))
        clock.now += 30
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "b"}))
        assert checks == [], "re-checked on the hot path"
        clock.now += 31
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "c"}))
        assert checks == ["r1"]
        verdict["value"] = "access"  # her seat is revoked
        clock.now += 61
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "d"}))
        clock.now += 1
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "e"}))
        frames = [json.loads(s) for s in ws.sent]
        assert [f["data"]["text"] for f in frames if f.get("type") == "run.token_delta"] == [
            "a",
            "b",
            "c",
        ]
        assert frames[-1] == {"type": "run.unwatched", "runId": "r1", "reason": "access"}
        assert checks == ["r1", "r1"], "a dropped watch is not re-checked again"
    finally:
        await mgr.close()


async def test_a_member_offboarded_mid_watch_has_the_socket_closed(redis_url: str) -> None:
    clock = _Clock()
    mgr = ConnectionManager(redis_url, clock=clock, watch_recheck_seconds=60.0)
    tenant = uuid.uuid4()

    async def recheck(run_id: str) -> str:
        return "gone"

    ws = ClosableWS()
    await mgr.connect(tenant, ws, member_id=uuid.uuid4(), feed=False, recheck=recheck)
    try:
        mgr.watch_run(tenant, ws, "r1")
        clock.now += 61
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "x"}))
        assert ws.sent == []
        assert ws.closed_with == 1008
        mgr.watch_run(tenant, ws, "r1")  # the socket is gone from the manager
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "y"}))
        assert ws.sent == []
    finally:
        await mgr.close()


async def test_a_failed_recheck_withholds_the_frame_and_retries(redis_url: str) -> None:
    """Fail closed on a database blip: the frame is not delivered, the watch
    is kept, and the next event re-checks again."""
    clock = _Clock()
    mgr = ConnectionManager(redis_url, clock=clock, watch_recheck_seconds=60.0)
    tenant = uuid.uuid4()
    calls = {"n": 0}

    async def recheck(run_id: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return "ok"

    ws = FakeWS()
    await mgr.connect(tenant, ws, member_id=uuid.uuid4(), feed=False, recheck=recheck)
    try:
        mgr.watch_run(tenant, ws, "r1")
        clock.now += 61
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "a"}))
        await mgr._broadcast(tenant, _env(tenant, "run.token_delta", {"run_id": "r1", "text": "b"}))
        assert [json.loads(s)["data"]["text"] for s in ws.sent] == ["b"]
        assert calls["n"] == 2
    finally:
        await mgr.close()
