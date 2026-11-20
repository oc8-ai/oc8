"""The prompt-cache split of a call: stored beside `tokens_in` by
record_usage, and billed at the cache rates (reads 0.1x, writes 1.25x of the
input price) instead of the full input price."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.metering.pricing import cost_micros_from_price
from oc8.metering.usage import record_usage
from tests.conftest import AppSessionFactory


async def test_record_usage_stores_the_cache_split(app_session: AppSessionFactory) -> None:
    tenant, request_id = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await record_usage(
            db,
            tenant_id=tenant,
            request_id=request_id,
            model="claude-sonnet-4-5",
            provider="anthropic",
            tokens_in=32100,
            tokens_out=7,
            cache_read_tokens=30000,
            cache_write_tokens=2000,
        )
    async with app_session(tenant) as db:
        rec = (
            await db.execute(
                select(m.TokenUsageRecord).where(m.TokenUsageRecord.request_id == request_id)
            )
        ).scalar_one()
    assert (rec.tokens_in, rec.cache_read_tokens, rec.cache_write_tokens) == (32100, 30000, 2000)


@pytest.mark.parametrize(
    ("read", "written", "expected"),
    [
        (0, 0, 1000 * 3.0 + 10 * 15.0),
        (900, 0, 100 * 3.0 + 900 * 3.0 * 0.1 + 10 * 15.0),
        (0, 900, 100 * 3.0 + 900 * 3.0 * 1.25 + 10 * 15.0),
        (600, 300, 100 * 3.0 + 600 * 0.3 + 300 * 3.75 + 10 * 15.0),
    ],
)
def test_cached_input_is_billed_at_the_cache_rates(
    read: int, written: int, expected: float
) -> None:
    cost = cost_micros_from_price(
        (3.0, 15.0), 1000, 10, cache_read_tokens=read, cache_write_tokens=written
    )
    assert cost == round(expected)
