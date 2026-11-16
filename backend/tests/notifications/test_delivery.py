# backend/tests/notifications/test_delivery.py
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from oc8 import models as m
from oc8.channels.dispatch import _CONTENT_WITHHELD
from oc8.channels.notice import ChannelCapabilities
from oc8.notifications import delivery, push
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_TITLE = ("Board available", "Dashboard verfügbar")
_BODY = ("Sales is now shared with you", "Vertrieb ist jetzt für dich freigegeben")


@pytest.fixture(autouse=True)
def _vapid(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("OC8_VAPID_PRIVATE_KEY", "priv")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeBus:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish_member_event(
        self,
        tenant_id: uuid.UUID,
        member_ids: Any,
        type_: str,
        data: dict[str, Any],
        *,
        source: str,
    ) -> None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "audience": list(member_ids),
                "type": type_,
                "data": data,
                "source": source,
            }
        )


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> _FakeBus:
    fake = _FakeBus()
    monkeypatch.setattr(delivery, "get_event_bus", lambda: fake)
    return fake


class _FakeChannel:
    def __init__(self, max_classification: str, *, fail: bool = False) -> None:
        self._caps = ChannelCapabilities(max_classification=max_classification)
        self.fail = fail
        self.said: list[tuple[str, str]] = []

    def capabilities(self) -> ChannelCapabilities:
        return self._caps

    async def say(self, external_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("messenger down")
        self.said.append((external_id, text))


def _channels(
    monkeypatch: pytest.MonkeyPatch, channels: dict[str, Any], bindings: dict[uuid.UUID, str]
) -> None:
    async def _channels_for_tenant(db: Any, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        return channels

    async def _binding(
        db: Any, *, tenant_id: uuid.UUID, member_id: uuid.UUID, channel: str
    ) -> str | None:
        return bindings.get(member_id)

    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", _channels_for_tenant)
    monkeypatch.setattr("oc8.copilot.followups.live_binding_external_id", _binding)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    calls: list[uuid.UUID] = []

    async def _fake_send_one(sub: m.PushSubscription, payload: dict[str, Any]) -> None:
        assert payload == {"title": _TITLE[1], "body": _BODY[1], "url": "/boards/1"}
        calls.append(sub.member_id)
        return None

    monkeypatch.setattr(push, "_send_one", _fake_send_one)
    return calls


async def _seed(app_session: AppSessionFactory, tenant: uuid.UUID, n: int) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with app_session(tenant) as db:
        for _ in range(n):
            unique = uuid.uuid4().hex
            member = m.OrgMember(tenant_id=tenant, subject=f"u-{unique}", subject_uuid=uuid.uuid4())
            db.add(member)
            await db.flush()
            db.add(
                m.PushSubscription(
                    tenant_id=tenant,
                    member_id=member.id,
                    endpoint=f"https://push.example.com/v1/{unique}",
                    p256dh="p",
                    auth="a",
                )
            )
            ids.append(member.id)
    return ids


async def _deliver(
    tenant: uuid.UUID, members: list[uuid.UUID], classification: str = "internal"
) -> None:
    await delivery.deliver(
        tenant_id=tenant,
        member_ids=members,
        kind="board.available",
        title=_TITLE,
        body=_BODY,
        link="/boards/1",
        classification=classification,
    )


async def test_push_goes_only_to_the_given_members(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[uuid.UUID],
    bus: _FakeBus,
) -> None:
    _channels(monkeypatch, {}, {})
    tenant = uuid.uuid4()
    a, b, _c = await _seed(app_session, tenant, 3)
    await _deliver(tenant, [a, b])
    assert sorted(sent) == sorted([a, b])


async def test_push_respects_the_members_setting(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[uuid.UUID],
    bus: _FakeBus,
) -> None:
    _channels(monkeypatch, {}, {})
    tenant = uuid.uuid4()
    a, b = await _seed(app_session, tenant, 2)
    async with app_session(tenant) as db:
        db.add(
            m.MemberNotificationSetting(
                tenant_id=tenant, member_id=a, kind="board.available", push_enabled=False
            )
        )
        # Another kind's opt-out does not apply.
        db.add(
            m.MemberNotificationSetting(
                tenant_id=tenant, member_id=b, kind="run.failed", push_enabled=False
            )
        )
    await _deliver(tenant, [a, b])
    assert sent == [b]


async def test_messenger_sends_german_text_and_respects_settings(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[uuid.UUID],
    bus: _FakeBus,
) -> None:
    tenant = uuid.uuid4()
    a, b, c = await _seed(app_session, tenant, 3)
    chan = _FakeChannel("internal")
    _channels(monkeypatch, {"telegram": chan}, {a: "ext-a", b: "ext-b"})
    async with app_session(tenant) as db:
        db.add(
            m.MemberNotificationSetting(
                tenant_id=tenant, member_id=b, kind="board.available", messenger_enabled=False
            )
        )
    await _deliver(tenant, [a, b, c])
    assert chan.said == [("ext-a", f"{_TITLE[1]}\n{_BODY[1]}")]


async def test_public_messenger_gets_the_withheld_sentence(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[uuid.UUID],
    bus: _FakeBus,
) -> None:
    tenant = uuid.uuid4()
    (a,) = await _seed(app_session, tenant, 1)
    chan = _FakeChannel("public")
    _channels(monkeypatch, {"telegram": chan}, {a: "ext-a"})
    await _deliver(tenant, [a])
    assert chan.said == [("ext-a", _CONTENT_WITHHELD)]


async def test_confidential_text_is_withheld_from_an_internal_messenger(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[uuid.UUID],
    bus: _FakeBus,
) -> None:
    tenant = uuid.uuid4()
    (a,) = await _seed(app_session, tenant, 1)
    chan = _FakeChannel("internal")
    _channels(monkeypatch, {"telegram": chan}, {a: "ext-a"})
    await _deliver(tenant, [a], classification="confidential")
    assert chan.said == [("ext-a", _CONTENT_WITHHELD)]


async def test_failures_never_raise_and_realtime_still_publishes(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, bus: _FakeBus
) -> None:
    tenant = uuid.uuid4()
    a, b = await _seed(app_session, tenant, 2)

    async def _boom(sub: m.PushSubscription, payload: dict[str, Any]) -> None:
        raise RuntimeError("push service down")

    monkeypatch.setattr(push, "_send_one", _boom)

    class _BrokenChannel(_FakeChannel):
        def capabilities(self) -> ChannelCapabilities:
            raise RuntimeError("plugin broken")

    ok = _FakeChannel("internal", fail=True)
    _channels(
        monkeypatch,
        {"broken": _BrokenChannel("internal"), "telegram": ok},
        {a: "ext-a", b: "ext-b"},
    )
    await _deliver(tenant, [a, b])
    assert len(bus.calls) == 1


async def test_failing_channel_lookup_does_not_raise(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[uuid.UUID],
    bus: _FakeBus,
) -> None:
    tenant = uuid.uuid4()
    (a,) = await _seed(app_session, tenant, 1)

    async def _boom(db: Any, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        raise RuntimeError("registry down")

    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", _boom)
    await _deliver(tenant, [a])
    assert sent == [a]
    assert len(bus.calls) == 1


async def test_realtime_event_goes_to_the_members(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[uuid.UUID],
    bus: _FakeBus,
) -> None:
    _channels(monkeypatch, {}, {})
    tenant = uuid.uuid4()
    a, b = await _seed(app_session, tenant, 2)
    await _deliver(tenant, [a, b])
    assert bus.calls == [
        {
            "tenant_id": tenant,
            "audience": [a, b],
            "type": "notification.created",
            "data": {"kind": "board.available"},
            "source": "notifications",
        }
    ]


async def test_realtime_event_is_published_before_push_and_messenger(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, bus: _FakeBus
) -> None:
    """The bell must not wait on a slow push service or messenger."""
    tenant = uuid.uuid4()
    (a,) = await _seed(app_session, tenant, 1)
    seen: list[str] = []

    async def _push(sub: m.PushSubscription, payload: dict[str, Any]) -> None:
        seen.append(f"push after {len(bus.calls)} realtime")

    class _Recording(_FakeChannel):
        async def say(self, external_id: str, text: str) -> None:
            seen.append(f"messenger after {len(bus.calls)} realtime")

    monkeypatch.setattr(push, "_send_one", _push)
    _channels(monkeypatch, {"telegram": _Recording("internal")}, {a: "ext-a"})
    await _deliver(tenant, [a])
    assert seen == ["push after 1 realtime", "messenger after 1 realtime"]


async def test_send_push_to_members_filters_by_member(
    app_session: AppSessionFactory, sent: list[uuid.UUID]
) -> None:
    tenant = uuid.uuid4()
    _a, b = await _seed(app_session, tenant, 2)
    async with app_session(tenant) as db:
        await push.send_push_to_members(
            db,
            tenant_id=tenant,
            member_ids=[b],
            payload={"title": _TITLE[1], "body": _BODY[1], "url": "/boards/1"},
        )
    assert sent == [b]


async def test_messenger_still_delivers_after_a_dead_push_subscription_is_pruned(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, bus: _FakeBus
) -> None:
    from sqlalchemy import select

    tenant = uuid.uuid4()
    (a,) = await _seed(app_session, tenant, 1)

    async def _dead(sub: m.PushSubscription, payload: dict[str, Any]) -> m.PushSubscription:
        return sub

    monkeypatch.setattr(push, "_send_one", _dead)
    seen: list[str | None] = []
    chan = _FakeChannel("internal")

    async def _channels_for_tenant(db: Any, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        return {"telegram": chan}

    async def _binding(
        db: Any, *, tenant_id: uuid.UUID, member_id: uuid.UUID, channel: str
    ) -> str | None:
        # A real lookup through RLS: it only sees rows when the tenant is bound
        # in the transaction this messenger step runs in.
        found = await db.scalar(select(m.OrgMember.id).where(m.OrgMember.id == member_id))
        seen.append(str(found) if found else None)
        return "ext-a" if found else None

    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", _channels_for_tenant)
    monkeypatch.setattr("oc8.copilot.followups.live_binding_external_id", _binding)
    await _deliver(tenant, [a])
    async with app_session(tenant) as db:
        assert (await db.execute(select(m.PushSubscription))).scalars().all() == []
    assert chan.said == [("ext-a", f"{_TITLE[1]}\n{_BODY[1]}")]
    assert len(bus.calls) == 1
