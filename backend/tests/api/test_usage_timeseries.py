"""GET /usage/timeseries -- tokens (in + out) per person over a recent range,
bucketed on the organisation's clock. The clock is frozen through
`feed._now` so bucket positions are exact; records carry explicit `ts`
values because `token_usage_record` is append-only."""

from __future__ import annotations

import datetime as dt
import itertools
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

import oc8.api.v1.feed as feed_mod
from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _freeze(monkeypatch: pytest.MonkeyPatch, now: dt.datetime) -> None:
    monkeypatch.setattr(feed_mod, "_now", lambda: now)


def _usage(
    tenant: uuid.UUID,
    ts: dt.datetime,
    member_id: uuid.UUID | None,
    tokens_in: int,
    tokens_out: int = 0,
) -> m.TokenUsageRecord:
    return m.TokenUsageRecord(
        tenant_id=tenant,
        request_id=uuid.uuid4(),
        model="claude-sonnet-4",
        provider="anthropic",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        member_id=member_id,
        ts=ts,
    )


def _member(tenant: uuid.UUID, name: str) -> m.OrgMember:
    return m.OrgMember(
        id=uuid.uuid4(),
        tenant_id=tenant,
        subject=f"{name}-{uuid.uuid4().hex[:6]}",
        subject_uuid=uuid.uuid4(),
        display_name=name,
    )


async def _berlin(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", timezone="Europe/Berlin"
            )
        )
        await db.commit()


async def _get(tenant: uuid.UUID, range_: str | None = None) -> dict[str, Any]:
    url = "/api/v1/usage/timeseries" + (f"?range={range_}" if range_ else "")
    async with _http() as http:
        resp = await http.get(url, headers=_headers(tenant))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_timeseries_requires_budget_view(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with _http() as http:
        resp = await http.get("/api/v1/usage/timeseries", headers=_headers(tenant, "member"))
    assert resp.status_code == 403, resp.text


async def test_timeseries_24h_hourly_per_member(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 12, 30, tzinfo=dt.UTC))
    async with app_session(tenant) as db:
        anna, ben = _member(tenant, "Anna"), _member(tenant, "Ben")
        db.add_all([anna, ben])
        await db.flush()
        db.add_all(
            [
                _usage(tenant, dt.datetime(2026, 11, 20, 11, 15, tzinfo=dt.UTC), anna.id, 100, 50),
                _usage(tenant, dt.datetime(2026, 11, 20, 11, 45, tzinfo=dt.UTC), anna.id, 10, 0),
                _usage(tenant, dt.datetime(2026, 11, 20, 12, 5, tzinfo=dt.UTC), anna.id, 1, 1),
                _usage(tenant, dt.datetime(2026, 11, 20, 12, 10, tzinfo=dt.UTC), ben.id, 7, 3),
                # Before the range's first hour: not counted.
                _usage(tenant, dt.datetime(2026, 11, 19, 12, 59, tzinfo=dt.UTC), ben.id, 999),
            ]
        )
        await db.commit()

    body = await _get(tenant, "24h")
    assert body["range"] == "24h"
    assert body["bucket"] == "hour"
    assert body["timezone"] == "UTC"
    assert len(body["buckets"]) == 24
    assert dt.datetime.fromisoformat(body["buckets"][0]) == dt.datetime(
        2026, 11, 19, 13, tzinfo=dt.UTC
    )
    assert dt.datetime.fromisoformat(body["buckets"][-1]) == dt.datetime(
        2026, 11, 20, 12, tzinfo=dt.UTC
    )
    by_key = {s["key"]: s for s in body["series"]}
    assert [s["key"] for s in body["series"]] == [str(anna.id), str(ben.id)]
    a, b = by_key[str(anna.id)], by_key[str(ben.id)]
    assert a["memberId"] == str(anna.id)
    assert a["name"] == "Anna"
    assert a["total"] == 162
    assert a["points"][22] == 160
    assert a["points"][23] == 2
    assert sum(a["points"]) == 162
    assert b["name"] == "Ben"
    assert b["total"] == 10
    assert b["points"][23] == 10
    assert len(a["points"]) == len(b["points"]) == 24


async def test_timeseries_defaults_to_7d(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 12, 30, tzinfo=dt.UTC))
    body = await _get(tenant)
    assert body["range"] == "7d"
    assert body["bucket"] == "hour"
    assert len(body["buckets"]) == 168


async def test_timeseries_30d_daily_in_org_timezone(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    await _berlin(app_session, tenant)
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 10, tzinfo=dt.UTC))
    member = uuid.uuid4()
    async with app_session(tenant) as db:
        # 23:30 UTC on the 18th is 00:30 on the 19th in Berlin.
        db.add(_usage(tenant, dt.datetime(2026, 11, 18, 23, 30, tzinfo=dt.UTC), member, 40))
        await db.commit()

    body = await _get(tenant, "30d")
    assert body["bucket"] == "day"
    assert body["timezone"] == "Europe/Berlin"
    assert len(body["buckets"]) == 30
    assert body["buckets"][-1] == "2026-11-20T00:00:00+01:00"
    assert body["buckets"][28] == "2026-11-19T00:00:00+01:00"
    (series,) = body["series"]
    assert series["points"][28] == 40
    assert series["points"][27] == 0


async def test_timeseries_buckets_across_dst(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Berlin leaves summer time on 2026-10-25: 03:00 CEST becomes 02:00 CET,
    so 02:00-03:00 happens twice. Both hours must be their own bucket, with
    their own offset, and no token may be lost or counted twice."""
    tenant = uuid.uuid4()
    await _berlin(app_session, tenant)
    # 13:10 CET on the 28th.
    _freeze(monkeypatch, dt.datetime(2026, 10, 28, 12, 10, tzinfo=dt.UTC))
    member = uuid.uuid4()
    seeded = [
        (dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC), 1),  # first bucket, 14:00 CEST
        (dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC), 20),  # 02:30 CEST
        (dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.UTC), 300),  # 02:30 CET
        (dt.datetime(2026, 10, 28, 12, 5, tzinfo=dt.UTC), 4000),  # the current hour
    ]
    async with app_session(tenant) as db:
        db.add_all(_usage(tenant, ts, member, tokens) for ts, tokens in seeded)
        await db.commit()

    body = await _get(tenant, "7d")
    buckets = [dt.datetime.fromisoformat(b) for b in body["buckets"]]
    # 167 wall-clock hours back from 13:00 CET is 14:00 CEST on the 21st: 168
    # real hours, so 169 buckets with the repeated hour.
    assert len(buckets) == 169
    assert len(set(body["buckets"])) == 169
    assert len({b.astimezone(dt.UTC) for b in buckets}) == 169
    assert all(
        (later - earlier) == dt.timedelta(hours=1) for earlier, later in itertools.pairwise(buckets)
    )
    first_two = body["buckets"].index("2026-10-25T02:00:00+02:00")
    second_two = body["buckets"].index("2026-10-25T02:00:00+01:00")
    assert second_two == first_two + 1
    (series,) = body["series"]
    assert series["points"][0] == 1
    assert series["points"][first_two] == 20
    assert series["points"][second_two] == 300
    assert series["points"][-1] == 4000
    assert sum(series["points"]) == series["total"] == sum(t for _, t in seeded)


async def test_timeseries_buckets_across_spring_dst(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Berlin enters summer time on 2026-03-29: 02:00 CET jumps to 03:00 CEST,
    so the 7-day range is one real hour shorter and 02:00 never appears."""
    tenant = uuid.uuid4()
    await _berlin(app_session, tenant)
    _freeze(monkeypatch, dt.datetime(2026, 4, 1, 10, 10, tzinfo=dt.UTC))  # 12:10 CEST
    member = uuid.uuid4()
    seeded = [
        (dt.datetime(2026, 3, 29, 0, 30, tzinfo=dt.UTC), 5),  # 01:30 CET
        (dt.datetime(2026, 3, 29, 1, 30, tzinfo=dt.UTC), 60),  # 03:30 CEST
    ]
    async with app_session(tenant) as db:
        db.add_all(_usage(tenant, ts, member, tokens) for ts, tokens in seeded)
        await db.commit()

    body = await _get(tenant, "7d")
    assert len(body["buckets"]) == 167
    assert len(set(body["buckets"])) == 167
    assert not any(b.startswith("2026-03-29T02:") for b in body["buckets"])
    one = body["buckets"].index("2026-03-29T01:00:00+01:00")
    assert body["buckets"][one + 1] == "2026-03-29T03:00:00+02:00"
    (series,) = body["series"]
    assert series["points"][one] == 5
    assert series["points"][one + 1] == 60
    assert sum(series["points"]) == 65


async def test_timeseries_folds_beyond_top_ten_into_others(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 12, 30, tzinfo=dt.UTC))
    members = [uuid.uuid4() for _ in range(12)]
    ts = dt.datetime(2026, 11, 20, 10, tzinfo=dt.UTC)
    async with app_session(tenant) as db:
        db.add_all(_usage(tenant, ts, member, 100 * (i + 1)) for i, member in enumerate(members))
        await db.commit()

    body = await _get(tenant, "24h")
    keys = [s["key"] for s in body["series"]]
    assert keys[:10] == [str(member) for member in reversed(members[2:])]
    assert keys[10:] == ["others"]
    totals = [s["total"] for s in body["series"][:10]]
    assert totals == sorted(totals, reverse=True)
    others = body["series"][10]
    assert others["memberId"] is None
    assert others["name"] is None
    assert others["total"] == 100 + 200
    assert others["points"][21] == 300


async def test_timeseries_no_person_series(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 12, 30, tzinfo=dt.UTC))
    async with app_session(tenant) as db:
        db.add(_usage(tenant, dt.datetime(2026, 11, 20, 12, tzinfo=dt.UTC), None, 30, 12))
        await db.commit()

    body = await _get(tenant, "24h")
    (series,) = body["series"]
    assert series["key"] == "none"
    assert series["memberId"] is None
    assert series["name"] is None
    assert series["total"] == 42
    assert series["points"][23] == 42


async def test_timeseries_unknown_member_has_no_name(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 12, 30, tzinfo=dt.UTC))
    gone = uuid.uuid4()
    async with app_session(tenant) as db:
        unnamed = _member(tenant, "")
        db.add(unnamed)
        await db.flush()
        db.add(_usage(tenant, dt.datetime(2026, 11, 20, 12, tzinfo=dt.UTC), gone, 9))
        db.add(_usage(tenant, dt.datetime(2026, 11, 20, 12, tzinfo=dt.UTC), unnamed.id, 8))
        await db.commit()

    body = await _get(tenant, "24h")
    by_key = {s["key"]: s for s in body["series"]}
    assert by_key[str(gone)]["memberId"] == str(gone)
    assert by_key[str(gone)]["name"] is None
    assert by_key[str(gone)]["total"] == 9
    # An empty display name is no name either, not "".
    assert by_key[str(unnamed.id)]["name"] is None


async def test_timeseries_empty_range(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 12, 30, tzinfo=dt.UTC))
    body = await _get(tenant, "30d")
    assert len(body["buckets"]) == 30
    assert body["series"] == []


async def test_timeseries_is_tenant_isolated(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    _freeze(monkeypatch, dt.datetime(2026, 11, 20, 12, 30, tzinfo=dt.UTC))
    ts = dt.datetime(2026, 11, 20, 12, tzinfo=dt.UTC)
    async with app_session(other) as db:
        db.add(_usage(other, ts, uuid.uuid4(), 500))
        db.add(_usage(other, ts, None, 500))
        await db.commit()
    async with app_session(tenant) as db:
        db.add(_usage(tenant, ts, None, 7))
        await db.commit()

    body = await _get(tenant, "24h")
    assert [(s["key"], s["total"]) for s in body["series"]] == [("none", 7)]


async def test_timeseries_rejects_unknown_range(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with _http() as http:
        resp = await http.get("/api/v1/usage/timeseries?range=1y", headers=_headers(tenant))
    assert resp.status_code == 422, resp.text
