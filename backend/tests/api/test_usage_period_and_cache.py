"""GET /usage: scoped to the caller's tenant by an explicit filter (not RLS
alone), limited to a period (this month in the organisation's timezone, or
all time), and reporting how much of the input came from the prompt cache."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from oc8 import models as m
from oc8.api.v1.feed import usage
from oc8.auth import Principal
from oc8.db.session import owner_session
from oc8.metering.budget import month_start
from tests.api.test_usage_endpoint import _headers, _http
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _rec(tenant: uuid.UUID, dept: uuid.UUID, **kw: object) -> m.TokenUsageRecord:
    return m.TokenUsageRecord(
        tenant_id=tenant,
        request_id=uuid.uuid4(),
        model="claude-sonnet-4",
        provider="anthropic",
        department_id=dept,
        **kw,
    )


async def test_usage_reports_the_cache_read_total_and_bills_it_cheaper(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.ModelPrice(
                provider="anthropic",
                model_pattern="claudesonnet4",
                price_in_usd_per_1m=10.0,
                price_out_usd_per_1m=30.0,
            )
        )
        db.add(
            _rec(
                tenant,
                dept,
                tokens_in=1000,
                tokens_out=10,
                cache_read_tokens=800,
                cache_write_tokens=100,
            )
        )
    async with _http() as http:
        resp = await http.get("/api/v1/usage", headers=_headers(tenant, "org_admin"))
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["group"] == str(dept))
    assert row["tokensIn"] == 1000
    assert row["cacheReadTokens"] == 800
    assert row["cacheWriteTokens"] == 100
    assert row["providerCostMicros"] == round(100 * 10 + 800 * 1.0 + 100 * 12.5 + 10 * 30)


async def test_usage_defaults_to_this_month_and_all_returns_everything(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept = uuid.uuid4(), uuid.uuid4()
    last_month = datetime.now(UTC).replace(day=1) - timedelta(days=20)
    async with app_session(tenant) as db:
        db.add(_rec(tenant, dept, tokens_in=5, tokens_out=0))
        db.add(_rec(tenant, dept, tokens_in=700, tokens_out=0, ts=last_month))
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        default = (await http.get("/api/v1/usage", headers=h)).json()
        month = (await http.get("/api/v1/usage?period=month", headers=h)).json()
        everything = (await http.get("/api/v1/usage?period=all", headers=h)).json()
        bad = await http.get("/api/v1/usage?period=week", headers=h)
    assert [r["tokensIn"] for r in default] == [5]
    assert [r["tokensIn"] for r in month] == [5]
    assert [r["tokensIn"] for r in everything] == [705]
    assert bad.status_code == 422


async def test_usage_sums_only_the_callers_tenant_even_without_rls(
    app_session: AppSessionFactory,
) -> None:
    mine, theirs, dept = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(mine) as db:
        db.add(_rec(mine, dept, tokens_in=3, tokens_out=0))
    async with app_session(theirs) as db:
        db.add(_rec(theirs, dept, tokens_in=4000, tokens_out=0))
    # The schema owner is not subject to RLS, so only the query's own filter
    # can keep the other tenant's rows out of the sum.
    async with owner_session() as db:
        rows = await usage(
            db, Principal(subject="u", tenant_id=mine, role="org_admin"), period="all"
        )
    assert [(r.group, r.tokens_in) for r in rows] == [(str(dept), 3)]


def test_month_start_is_read_in_the_organisations_timezone() -> None:
    late_evening = datetime(2026, 11, 30, 23, 30, tzinfo=UTC)  # already 1 Dec in Berlin
    assert month_start("Europe/Berlin", now=late_evening) == datetime(
        2026, 11, 30, 23, 0, tzinfo=UTC
    )
    assert month_start("UTC", now=late_evening) == datetime(2026, 11, 1, tzinfo=UTC)
