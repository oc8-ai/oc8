"""GET /usage/export -- a per-record CSV/JSONL export of TokenUsageRecord,
mirroring GET /audit/export's streaming shape (see api/v1/audit.py). Unlike
that endpoint, TokenUsageRecord has no `seq`-style identity column, so the
one test that matters here is the page-boundary test: a ts-only cursor at a
batch boundary landing inside a group of identical timestamps would either
repeat rows forever or skip them. The export cursor must be the composite
(ts, id).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def _get(app: FastAPI, url: str, tenant: uuid.UUID, role: str = "org_admin") -> Response:
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get(url, headers={"Authorization": f"Bearer {_token(tenant, role)}"})


async def _seed(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    n: int,
    *,
    agent_name: str | None = None,
    same_ts: datetime | None = None,
) -> None:
    async with app_session(tenant) as s:
        agent_id = None
        if agent_name is not None:
            dept = m.Department(tenant_id=tenant, name="Sales", goal="", frame={}, presentation={})
            s.add(dept)
            await s.flush()
            agent = m.Agent(tenant_id=tenant, department_id=dept.id, name=agent_name)
            s.add(agent)
            await s.flush()
            agent_id = agent.id
        for i in range(n):
            kwargs: dict[str, Any] = dict(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model="claude-sonnet-4",
                provider="anthropic",
                tokens_in=100 + i,
                tokens_out=50 + i,
                agent_id=agent_id,
            )
            if same_ts is not None:
                kwargs["ts"] = same_ts
            s.add(m.TokenUsageRecord(**kwargs))
        await s.commit()


async def test_csv_and_jsonl_have_identical_keys_and_row_count(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant, 3)
    app = create_app()
    csv_res = await _get(app, "/api/v1/usage/export?format=csv", tenant)
    jsonl_res = await _get(app, "/api/v1/usage/export?format=jsonl", tenant)
    assert csv_res.status_code == 200, csv_res.text
    assert jsonl_res.status_code == 200, jsonl_res.text
    header = csv_res.text.splitlines()[0].split(",")
    first = json.loads(jsonl_res.text.splitlines()[0])
    assert set(header) == set(first)
    assert len(csv_res.text.splitlines()) - 1 == len(jsonl_res.text.splitlines()) == 3


async def test_agent_name_cannot_inject_a_spreadsheet_formula(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant, 1, agent_name="=cmd|'/c calc'!A1")
    r = await _get(create_app(), "/api/v1/usage/export?format=csv", tenant)
    assert r.status_code == 200, r.text
    assert "\"'=cmd" in r.text or ",'=cmd" in r.text


async def test_page_boundary_with_identical_timestamps_neither_repeats_nor_skips(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.api.v1.feed as feed_mod

    monkeypatch.setattr(feed_mod, "BATCH", 2)
    assert feed_mod.BATCH == 2

    tenant = uuid.uuid4()
    same_ts = datetime(2026, 1, 1, tzinfo=UTC)
    # BATCH+1 records sharing one ts -- a ts-only cursor would loop or lose rows.
    await _seed(app_session, tenant, 3, same_ts=same_ts)
    r = await _get(create_app(), "/api/v1/usage/export?format=jsonl", tenant)
    assert r.status_code == 200, r.text
    rows = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    request_ids = [row["requestId"] for row in rows]
    assert len(request_ids) == 3
    assert len(set(request_ids)) == 3


async def test_empty_export_is_a_valid_empty_document(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    r = await _get(create_app(), "/api/v1/usage/export?format=csv", tenant)
    assert r.status_code == 200
    assert r.text.strip().startswith("ts,")


async def test_member_is_forbidden(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant, 1)
    r = await _get(create_app(), "/api/v1/usage/export?format=csv", tenant, role="member")
    assert r.status_code == 403


async def test_export_rejects_invalid_format(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    r = await _get(create_app(), "/api/v1/usage/export?format=xml", tenant)
    assert r.status_code == 422


async def test_export_honours_agent_id_filter(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Sales", goal="", frame={}, presentation={})
        s.add(dept)
        await s.flush()
        agent_a = m.Agent(tenant_id=tenant, department_id=dept.id, name="Ada")
        agent_b = m.Agent(tenant_id=tenant, department_id=dept.id, name="Bob")
        s.add_all([agent_a, agent_b])
        await s.flush()
        s.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model="claude-sonnet-4",
                provider="anthropic",
                tokens_in=1,
                tokens_out=1,
                agent_id=agent_a.id,
            )
        )
        s.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model="claude-sonnet-4",
                provider="anthropic",
                tokens_in=1,
                tokens_out=1,
                agent_id=agent_b.id,
            )
        )
        await s.commit()
        target_agent_id = agent_a.id
    r = await _get(
        create_app(), f"/api/v1/usage/export?format=jsonl&agent_id={target_agent_id}", tenant
    )
    assert r.status_code == 200, r.text
    rows = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0]["agentId"] == str(target_agent_id)
    assert rows[0]["agentName"] == "Ada"


async def test_cost_micros_prices_each_row_at_its_own_time_not_todays_rate(
    app_session: AppSessionFactory,
) -> None:
    """Regression coverage for `export_usage`'s per-row `price_as_of` call.

    `GET /usage`'s own historical-accuracy test
    (`test_usage_uses_the_price_in_effect_at_usage_time_not_today`) exercises
    the day-bucket aggregator, a different code path from this export's
    per-row streaming loop -- it proves nothing about whether `export_usage`
    itself prices correctly. Two `ModelPrice` versions straddle two
    `TokenUsageRecord` timestamps here; a bug that priced every row at "now"
    (or all rows at the same version) would make both rows' `costMicros`
    equal, which this asserts against directly.

    A fake, export-test-only provider/model keeps this independent of
    whatever other `ModelPrice` rows other tests have left in this
    session-scoped database (see conftest.py -- `model_price` is global, not
    tenant-scoped, and the DB is not reset between tests).
    """
    tenant = uuid.uuid4()
    provider = "usage-export-pricing-test-provider"
    model = "usage-export-pricing-test-model"
    pattern = "usageexportpricingtestmodel"
    old_price = m.ModelPrice(
        provider=provider,
        model_pattern=pattern,
        price_in_usd_per_1m=3.0,
        price_out_usd_per_1m=15.0,
        effective_from=datetime(2020, 1, 1, tzinfo=UTC),
    )
    new_price = m.ModelPrice(
        provider=provider,
        model_pattern=pattern,
        price_in_usd_per_1m=99.0,
        price_out_usd_per_1m=199.0,
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    old_ts = datetime(2021, 6, 1, tzinfo=UTC)  # after old_price, before new_price
    new_ts = datetime(2026, 6, 1, tzinfo=UTC)  # after new_price
    async with app_session(tenant) as s:
        s.add_all([old_price, new_price])
        await s.flush()
        s.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model=model,
                provider=provider,
                tokens_in=1000,
                tokens_out=500,
                ts=old_ts,
            )
        )
        s.add(
            m.TokenUsageRecord(
                tenant_id=tenant,
                request_id=uuid.uuid4(),
                model=model,
                provider=provider,
                tokens_in=1000,
                tokens_out=500,
                ts=new_ts,
            )
        )
        await s.commit()

    r = await _get(create_app(), "/api/v1/usage/export?format=jsonl", tenant)
    assert r.status_code == 200, r.text
    rows = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    assert len(rows) == 2
    rows.sort(key=lambda row: row["ts"])
    old_row, new_row = rows

    expected_old_cost = round(1000 * 3.0 + 500 * 15.0)
    expected_new_cost = round(1000 * 99.0 + 500 * 199.0)
    assert old_row["costMicros"] == expected_old_cost
    assert new_row["costMicros"] == expected_new_cost
    assert old_row["costMicros"] != new_row["costMicros"]
