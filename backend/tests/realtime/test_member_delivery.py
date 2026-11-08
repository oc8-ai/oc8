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
