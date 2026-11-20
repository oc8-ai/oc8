"""GET /usage -- per-department/per-agent token and cost aggregates from
TokenUsageRecord, including what department prompt caching saved. Cost is
computed lazily from a versioned price table (see metering/pricing.py) --
these tests seed model_price rows explicitly so cost assertions are
deterministic."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def test_usage_includes_savings_aggregates(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            id=dept_id, tenant_id=tenant, name="Sales", goal="", frame={}, presentation={}
        )
        db.add(dept)
        db.add(
            m.ModelPrice(
                provider="anthropic",
                model_pattern="claudesonnet4",
                price_in_usd_per_1m=10.0,
                price_out_usd_per_1m=30.0,
            )
        )
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model="claude-sonnet-4",
                provider="anthropic",
                tokens_in=0,
                tokens_out=0,
                department_id=dept_id,
                cache_hit=True,
                saved_tokens_in=100,
                saved_tokens_out=50,
            )
        )
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model="claude-sonnet-4",
                provider="anthropic",
                tokens_in=200,
                tokens_out=80,
                department_id=dept_id,
            )
        )
        await db.commit()
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.get("/api/v1/usage?group_by=department", headers=h)
        assert resp.status_code == 200, resp.text
        row = next(r for r in resp.json() if r["group"] == str(dept_id))
        assert row["savedTokensIn"] == 100
        assert row["savedTokensOut"] == 50
        assert row["savedCostMicros"] == round(100 * 10.0 + 50 * 30.0)
        assert row["tokensIn"] == 200
        assert row["tokensOut"] == 80
        assert row["providerCostMicros"] == round(200 * 10.0 + 80 * 30.0)


async def test_usage_defaults_savings_to_zero_with_no_cache_hits(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            id=dept_id, tenant_id=tenant, name="Sales", goal="", frame={}, presentation={}
        )
        db.add(dept)
        db.add(
            m.ModelPrice(
                provider="anthropic",
                model_pattern="claudesonnet4",
                price_in_usd_per_1m=10.0,
                price_out_usd_per_1m=30.0,
            )
        )
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model="claude-sonnet-4",
                provider="anthropic",
                tokens_in=200,
                tokens_out=80,
                department_id=dept_id,
            )
        )
        await db.commit()
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.get("/api/v1/usage?group_by=department", headers=h)
        assert resp.status_code == 200, resp.text
        row = next(r for r in resp.json() if r["group"] == str(dept_id))
        assert row["savedTokensIn"] == 0
        assert row["savedTokensOut"] == 0
        assert row["savedCostMicros"] == 0


async def test_usage_uses_the_price_in_effect_at_usage_time_not_today(
    app_session: AppSessionFactory,
) -> None:
    """The core historical-accuracy contract from the spec: editing a price
    today must not change what a report shows for usage recorded before the
    edit."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            id=dept_id, tenant_id=tenant, name="Sales", goal="", frame={}, presentation={}
        )
        db.add(dept)
        old_price = m.ModelPrice(
            provider="anthropic",
            model_pattern="claudesonnet4",
            price_in_usd_per_1m=3.0,
            price_out_usd_per_1m=15.0,
            effective_from=dt.datetime.now(dt.UTC) - dt.timedelta(days=30),
        )
        db.add(old_price)
        await db.flush()
        # Usage recorded 20 days ago -- before today's price edit below.
        # `token_usage_record` is append-only (UPDATE/DELETE revoked from the
        # runtime role since migration 0001), so the backdated ts must be part
        # of the INSERT itself, not set on the row after it's already flushed.
        old_usage = m.TokenUsageRecord(
            tenant_id=tenant,
            request_id=uuid.uuid4(),
            model="claude-sonnet-4",
            provider="anthropic",
            tokens_in=1000,
            tokens_out=500,
            department_id=dept_id,
            ts=dt.datetime.now(dt.UTC) - dt.timedelta(days=20),
        )
        db.add(old_usage)
        await db.flush()
        # A price edit made today -- a NEW row, the old one is untouched.
        db.add(
            m.ModelPrice(
                provider="anthropic",
                model_pattern="claudesonnet4",
                price_in_usd_per_1m=99.0,
                price_out_usd_per_1m=99.0,
                effective_from=dt.datetime.now(dt.UTC),
            )
        )
        await db.commit()
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.get("/api/v1/usage?group_by=department&period=all", headers=h)
        row = next(r for r in resp.json() if r["group"] == str(dept_id))
        # Must use the OLD price (3.0/15.0), not today's 99.0/99.0.
        assert row["providerCostMicros"] == round(1000 * 3.0 + 500 * 15.0)


async def test_usage_prices_historical_records_against_the_backdated_seed_price(
    app_session: AppSessionFactory,
) -> None:
    """Regression test for the Task 14 finding: migrations 0060/0062 seeded
    model_price rows without an explicit effective_from, defaulting to
    now() at migration-apply time -- so any TokenUsageRecord dated before a
    database's 0060/0062 apply moment could never match and permanently
    priced at $0/unknown. Migration 0065 backdates the 6 baseline seed rows'
    effective_from to 2020-01-01. This uses the REAL, migration-seeded
    global `anthropic`/`claudesonnet45` row (not a fake tenant-local one,
    since model_price is global, not tenant-scoped) with a real Claude 4.x
    model id, dated well before this test suite runs (2025-06-01, safely
    after the 2020-01-01 backdate but long before "today") -- simulating
    usage that happened before this fix was ever deployed. Before the 0065
    fix, this would have priced at $0 because the seed row's effective_from
    would have been "whenever the test DB's migrations ran" (today), which
    is after this record's ts."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            id=dept_id, tenant_id=tenant, name="Sales", goal="", frame={}, presentation={}
        )
        db.add(dept)
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model="claude-sonnet-4-5-20250929",
                provider="anthropic",
                tokens_in=1000,
                tokens_out=500,
                department_id=dept_id,
                ts=dt.datetime(2025, 6, 1, tzinfo=dt.UTC),
            )
        )
        await db.commit()
    async with _http() as http:
        h = _headers(tenant, "org_admin")
        resp = await http.get("/api/v1/usage?group_by=department&period=all", headers=h)
        assert resp.status_code == 200, resp.text
        row = next(r for r in resp.json() if r["group"] == str(dept_id))
        # The real seeded claudesonnet45 price (3.0 in / 15.0 out per 1M),
        # not $0 -- only reachable if the seed row's effective_from is
        # actually backdated far enough to cover this historical record.
        assert row["providerCostMicros"] == round(1000 * 3.0 + 500 * 15.0)


async def test_usage_requires_budget_view_permission(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant):
        pass
    async with _http() as http:
        h = _headers(tenant, "member")
        resp = await http.get("/api/v1/usage?group_by=department", headers=h)
        assert resp.status_code == 403, resp.text
