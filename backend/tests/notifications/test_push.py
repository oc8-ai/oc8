# backend/tests/notifications/test_push.py
from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.notifications import push
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"

    async def text(self) -> str:
        return "test body"


async def _seed_subscription(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, str]:
    # `endpoint` (uq_push_subscription_endpoint) and `subject`
    # (uq_org_member_subject) are both unique WITHIN a tenant, and callers here
    # reuse a tenant; app_session also commits its writes rather than rolling
    # them back -- so every call needs its own values, not shared literals, to
    # stay independent of every other call (including a second call within the
    # same test, and the DB's prior state from earlier test runs).
    unique = uuid.uuid4().hex
    endpoint = f"https://push.example.com/v1/{unique}"
    async with app_session(tenant) as db:
        member = m.OrgMember(tenant_id=tenant, subject=f"u-{unique}", subject_uuid=uuid.uuid4())
        db.add(member)
        await db.flush()
        sub = m.PushSubscription(
            tenant_id=tenant,
            member_id=member.id,
            endpoint=endpoint,
            p256dh="p256dh-key",
            auth="auth-key",
        )
        db.add(sub)
        await db.flush()
        sub_id = sub.id
    return sub_id, endpoint


async def test_send_push_for_tenant_is_a_noop_without_vapid_keys(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("OC8_VAPID_PUBLIC_KEY", "")
    monkeypatch.setenv("OC8_VAPID_PRIVATE_KEY", "")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    await _seed_subscription(app_session, tenant)

    called = False

    async def _fake_webpush_async(**kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(push, "webpush_async", _fake_webpush_async)
    async with app_session(tenant) as db:
        await push.send_push_for_tenant(
            db, tenant_id=tenant, payload={"title": "t", "body": "b", "url": "/x"}
        )
    assert called is False
    get_settings.cache_clear()


async def test_send_push_for_tenant_sends_to_every_subscription(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("OC8_VAPID_PRIVATE_KEY", "priv")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    _sub_id, endpoint = await _seed_subscription(app_session, tenant)

    calls: list[dict[str, Any]] = []

    async def _fake_webpush_async(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(push, "webpush_async", _fake_webpush_async)
    async with app_session(tenant) as db:
        await push.send_push_for_tenant(
            db, tenant_id=tenant, payload={"title": "t", "body": "b", "url": "/x"}
        )
    assert len(calls) == 1
    assert calls[0]["subscription_info"]["endpoint"] == endpoint
    assert calls[0]["subscription_info"]["keys"] == {"p256dh": "p256dh-key", "auth": "auth-key"}
    # A TTL is not optional: pywebpush's default of 0 means "deliver now or
    # discard", which would defeat the point of reaching a closed browser. And
    # a timeout is not optional either: the caller holds a pooled DB connection
    # open across the whole gather, so aiohttp's 5-minute default would park it.
    assert calls[0]["ttl"] == 86400
    assert calls[0]["timeout"] == 10
    get_settings.cache_clear()


async def test_send_push_for_one_member_reaches_only_their_devices(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3a: a member's own approval is pushed to that member's browsers, never
    to a colleague's in the same tenant."""
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("OC8_VAPID_PRIVATE_KEY", "priv")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    anna_sub, anna_endpoint = await _seed_subscription(app_session, tenant)
    await _seed_subscription(app_session, tenant)
    async with app_session(tenant) as db:
        row = await db.get(m.PushSubscription, anna_sub)
        assert row is not None
        anna = row.member_id

    calls: list[dict[str, Any]] = []

    async def _fake_webpush_async(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(push, "webpush_async", _fake_webpush_async)
    async with app_session(tenant) as db:
        await push.send_push_for_tenant(
            db, tenant_id=tenant, payload={"title": "t", "body": "b", "url": "/x"}, member_id=anna
        )
    assert [c["subscription_info"]["endpoint"] for c in calls] == [anna_endpoint]
    get_settings.cache_clear()


async def test_send_push_for_tenant_prunes_410_subscription(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("OC8_VAPID_PRIVATE_KEY", "priv")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    sub_id, _endpoint = await _seed_subscription(app_session, tenant)

    async def _fake_webpush_async(**kwargs: Any) -> None:
        raise push.WebPushException("gone", response=_FakeResponse(410))

    monkeypatch.setattr(push, "webpush_async", _fake_webpush_async)
    async with app_session(tenant) as db:
        await push.send_push_for_tenant(
            db, tenant_id=tenant, payload={"title": "t", "body": "b", "url": "/x"}
        )
        assert await db.get(m.PushSubscription, sub_id) is None
    get_settings.cache_clear()


async def test_send_push_for_tenant_prunes_multiple_dead_subscriptions_concurrently(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two subscriptions on the same tenant both go 410 at once. `_send_one`'s
    concurrent network calls (via `asyncio.gather`) must not translate into
    concurrent `db.delete`/`db.commit` calls on the single shared `AsyncSession`
    -- SQLAlchemy's AsyncSession is not safe for concurrent use, and a real
    Postgres round trip inside `db.delete`/`db.commit` gives the event loop a
    genuine chance to interleave the two prunes if they were issued from
    concurrently-scheduled tasks. Both subscriptions must still end up pruned,
    and `send_push_for_tenant` must not raise."""
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("OC8_VAPID_PRIVATE_KEY", "priv")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    sub_id_1, _e1 = await _seed_subscription(app_session, tenant)
    sub_id_2, _e2 = await _seed_subscription(app_session, tenant)

    async def _fake_webpush_async(**kwargs: Any) -> None:
        raise push.WebPushException("gone", response=_FakeResponse(410))

    monkeypatch.setattr(push, "webpush_async", _fake_webpush_async)
    async with app_session(tenant) as db:
        await push.send_push_for_tenant(
            db, tenant_id=tenant, payload={"title": "t", "body": "b", "url": "/x"}
        )
        assert await db.get(m.PushSubscription, sub_id_1) is None
        assert await db.get(m.PushSubscription, sub_id_2) is None
    get_settings.cache_clear()


async def test_send_push_for_tenant_keeps_subscription_on_other_errors(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.config import get_settings

    monkeypatch.setenv("OC8_VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("OC8_VAPID_PRIVATE_KEY", "priv")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    sub_id, _endpoint = await _seed_subscription(app_session, tenant)

    async def _fake_webpush_async(**kwargs: Any) -> None:
        raise push.WebPushException("server error", response=_FakeResponse(500))

    monkeypatch.setattr(push, "webpush_async", _fake_webpush_async)
    async with app_session(tenant) as db:
        await push.send_push_for_tenant(
            db, tenant_id=tenant, payload={"title": "t", "body": "b", "url": "/x"}
        )
        assert await db.get(m.PushSubscription, sub_id) is not None
    get_settings.cache_clear()
