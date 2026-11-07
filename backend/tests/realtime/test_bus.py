from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import redis.asyncio as redis

from oc8.realtime.bus import EventBus, channel_for
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_publish_event_delivers_to_tenant_channel(redis_url: str) -> None:
    tenant_id = uuid.uuid4()
    bus = EventBus(redis_url)
    sub = redis.from_url(redis_url, decode_responses=True)
    pubsub = sub.pubsub()
    await pubsub.subscribe(channel_for(tenant_id))
    # drain the subscribe confirmation
    await pubsub.get_message(timeout=1.0)
    try:
        await bus.publish_event(
            tenant_id,
            "agent.status",
            {"agent_id": "a1", "status": "running"},
            source="oc8/agent/a1",
        )
        msg = None
        for _ in range(20):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if msg is not None:
                break
        assert msg is not None, "no message received on the tenant channel"
        env = json.loads(msg["data"])
        assert env["type"] == "agent.status"
        assert env["tenantid"] == str(tenant_id)
        assert env["data"]["status"] == "running"
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await sub.aclose()
        await bus.close()


async def test_publish_event_swallows_redis_failure() -> None:
    # A bus pointed at a dead port must not raise into the caller.
    bus = EventBus("redis://127.0.0.1:1/0")
    await bus.publish_event(uuid.uuid4(), "x", {}, source="s")  # must not raise
    await bus.close()


async def test_publish_event_triggers_push_for_approval_created(
    redis_url: str, monkeypatch: pytest.MonkeyPatch, app_session: AppSessionFactory
) -> None:
    from oc8 import models as m
    from oc8.realtime import bus as bus_module

    tenant_id = uuid.uuid4()
    async with app_session(tenant_id) as db:
        approval = m.ApprovalRequest(
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            action_type="tool_call",
            title="Neue Freigabe",
            detail="Ein Agent wartet auf deine Entscheidung.",
        )
        db.add(approval)
        await db.flush()
        approval_id = approval.id

    calls: list[dict[str, object]] = []

    async def _fake_send_push_for_tenant(db: object, *, tenant_id: object, payload: dict) -> None:
        calls.append({"tenant_id": tenant_id, "payload": payload})

    monkeypatch.setattr(bus_module, "send_push_for_tenant", _fake_send_push_for_tenant)

    bus = bus_module.EventBus(redis_url)
    try:
        await bus.publish_event(
            tenant_id,
            "approval.created",
            {"approval_id": str(approval_id)},
            source="test",
        )
        await asyncio.gather(*bus._background_tasks)
        assert len(calls) == 1
        assert calls[0]["tenant_id"] == tenant_id
        assert calls[0]["payload"] == {
            "title": "Neue Freigabe",
            "body": "Ein Agent wartet auf deine Entscheidung.",
            "url": "/workspace",
        }
    finally:
        await bus.close()


async def test_publish_event_pushes_from_envelope_without_a_committed_row(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three in-process publishers (engine, ask_human, budget breach) publish
    BEFORE their caller commits, so a fresh session's `db.get` cannot see the
    row. The payload must come out of the envelope instead -- no DB row here at
    all, which is exactly that situation taken to its limit."""
    from oc8.realtime import bus as bus_module

    tenant_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    calls: list[dict[str, object]] = []

    async def _fake_send_push_for_tenant(db: object, *, tenant_id: object, payload: dict) -> None:
        calls.append({"tenant_id": tenant_id, "payload": payload})

    monkeypatch.setattr(bus_module, "send_push_for_tenant", _fake_send_push_for_tenant)

    bus = bus_module.EventBus(redis_url)
    try:
        await bus.publish_event(
            tenant_id,
            "approval.created",
            {
                "approval_id": str(approval_id),
                "action_type": "tool_send",
                "status": "pending",
                "title": "Nora wants to call crm_create_lead",
                "detail": "Betrag über 3000 €",
            },
            source="test",
        )
        await asyncio.gather(*bus._background_tasks)
        assert len(calls) == 1
        assert calls[0]["payload"] == {
            "title": "Nora wants to call crm_create_lead",
            "body": "Betrag über 3000 €",
            "url": "/workspace",
        }
    finally:
        await bus.close()


async def test_publish_event_falls_back_to_the_default_title_for_an_empty_envelope_title(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.realtime import bus as bus_module

    tenant_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    calls: list[dict[str, object]] = []

    async def _fake_send_push_for_tenant(db: object, *, tenant_id: object, payload: dict) -> None:
        calls.append({"tenant_id": tenant_id, "payload": payload})

    monkeypatch.setattr(bus_module, "send_push_for_tenant", _fake_send_push_for_tenant)

    bus = bus_module.EventBus(redis_url)
    try:
        await bus.publish_event(
            tenant_id,
            "approval.created",
            {"approval_id": str(approval_id), "title": "", "detail": None},
            source="test",
        )
        await asyncio.gather(*bus._background_tasks)
        assert len(calls) == 1
        payload = calls[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["title"] == bus_module.DEFAULT_PUSH_TITLE
        assert payload["body"] is None
    finally:
        await bus.close()


async def test_publish_event_sends_no_push_when_neither_envelope_nor_row_has_content(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `db.get` fallback still guards an approval_id that resolves to nothing."""
    from oc8.realtime import bus as bus_module

    called = False

    async def _fake_send_push_for_tenant(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(bus_module, "send_push_for_tenant", _fake_send_push_for_tenant)

    bus = bus_module.EventBus(redis_url)
    try:
        await bus.publish_event(
            uuid.uuid4(),
            "approval.created",
            {"approval_id": str(uuid.uuid4())},
            source="test",
        )
        await asyncio.gather(*bus._background_tasks)
        assert called is False
    finally:
        await bus.close()


async def test_publish_event_does_not_trigger_push_for_other_types(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.realtime import bus as bus_module

    called = False

    async def _fake_send_push_for_tenant(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(bus_module, "send_push_for_tenant", _fake_send_push_for_tenant)

    bus = bus_module.EventBus(redis_url)
    try:
        await bus.publish_event(uuid.uuid4(), "agent.status", {"agent_id": "a1"}, source="test")
        await asyncio.gather(*bus._background_tasks)
        assert called is False
    finally:
        await bus.close()


async def test_a_members_own_approval_is_pushed_to_them_and_kept_off_the_socket(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3a: an approval addressed to one member reaches that member's devices
    only, and the tenant-wide socket carries no title or detail for it."""
    from oc8.realtime import bus as bus_module

    tenant_id = uuid.uuid4()
    anna = uuid.uuid4()
    calls: list[dict[str, object]] = []

    async def _fake_send_push_for_tenant(
        db: object, *, tenant_id: object, payload: dict, member_id: object = None
    ) -> None:
        calls.append({"payload": payload, "member_id": member_id})

    monkeypatch.setattr(bus_module, "send_push_for_tenant", _fake_send_push_for_tenant)
    sub = redis.from_url(redis_url, decode_responses=True)
    pubsub = sub.pubsub()
    await pubsub.subscribe(channel_for(tenant_id))
    await pubsub.get_message(timeout=1.0)
    bus = bus_module.EventBus(redis_url)
    try:
        await bus.publish_event(
            tenant_id,
            "approval.created",
            {
                "approval_id": str(uuid.uuid4()),
                "action_type": "tool_send",
                "status": "pending",
                "title": "Nora wants to call gmail_send",
                "detail": "your own rule asks you",
                "assignee_member_id": str(anna),
            },
            source="test",
        )
        await asyncio.gather(*bus._background_tasks)
        msg = None
        for _ in range(20):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if msg is not None:
                break
        assert msg is not None
        data = json.loads(msg["data"])["data"]
        assert "title" not in data and "detail" not in data
        assert data["assignee_member_id"] == str(anna)
        assert calls == [
            {
                "payload": {
                    "title": "Nora wants to call gmail_send",
                    "body": "your own rule asks you",
                    "url": "/workspace",
                },
                "member_id": anna,
            }
        ]
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await sub.aclose()
        await bus.close()
