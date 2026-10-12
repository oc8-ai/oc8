from __future__ import annotations

import datetime as dt
import uuid

import pytest
from oc8 import models as m
from oc8.metering.provider_budget import (
    check_provider_budget,
    get_provider_budget,
    provider_month_tokens,
    provider_tokens_this_month,
    set_provider_budget,
)
from oc8.metering.usage import record_usage
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _use(db, tenant: uuid.UUID, provider: str, tokens_in: int, tokens_out: int = 0) -> uuid.UUID:
    request_id = uuid.uuid4()
    await record_usage(
        db, tenant_id=tenant, request_id=request_id, model="m", provider=provider,
        tokens_in=tokens_in, tokens_out=tokens_out,
    )
    return request_id


async def test_set_provider_budget_upserts_one_row_per_provider(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await set_provider_budget(db, tenant_id=tenant, provider="opaas_ai", monthly_token_limit=1000)
        row = await set_provider_budget(db, tenant_id=tenant, provider="opaas_ai", monthly_token_limit=2000)
        assert row.provider == "opaas_ai"
        found = await get_provider_budget(db, tenant_id=tenant, provider="opaas_ai")
        assert found is not None and found.id == row.id and found.monthly_token_limit == 2000


async def test_provider_month_tokens_counts_only_that_provider(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _use(db, tenant, "opaas_ai", 100, 50)
        await _use(db, tenant, "anthropic", 999)
        assert await provider_month_tokens(db, tenant_id=tenant, provider="opaas_ai") == 150
        assert await provider_tokens_this_month(db, tenant_id=tenant) == {"opaas_ai": 150, "anthropic": 999}


async def test_provider_month_tokens_ignores_last_month(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # token_usage_record is append-only (UPDATE is revoked), so the old
        # row is written with its timestamp instead of moved back.
        last_month = dt.datetime.now(tz=dt.UTC).replace(day=1) - dt.timedelta(days=2)
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant, request_id=uuid.uuid4(), model="m", provider="opaas_ai",
                tokens_in=500, tokens_out=0, ts=last_month,
            )
        )
        await db.flush()
        await _use(db, tenant, "opaas_ai", 7)
        assert await provider_month_tokens(db, tenant_id=tenant, provider="opaas_ai") == 7


async def test_check_reports_exhausted_at_the_limit(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await set_provider_budget(db, tenant_id=tenant, provider="opaas_ai", monthly_token_limit=100)
        await _use(db, tenant, "opaas_ai", 60, 40)
        check = await check_provider_budget(db, tenant_id=tenant, provider="opaas_ai")
        assert check.exhausted is True and check.used == 100 and check.limit == 100
        assert check.resets_at.day == 1 and check.resets_at > dt.datetime.now(tz=dt.UTC)


async def test_no_row_or_null_limit_is_never_exhausted(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _use(db, tenant, "opaas_ai", 10_000)
        assert (await check_provider_budget(db, tenant_id=tenant, provider="opaas_ai")).exhausted is False
        await set_provider_budget(db, tenant_id=tenant, provider="opaas_ai", monthly_token_limit=None)
        assert (await check_provider_budget(db, tenant_id=tenant, provider="opaas_ai")).exhausted is False
