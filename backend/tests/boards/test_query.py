"""The query engine: effective batches, aggregation, compare, rows and notes."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import oc8.models as m
from oc8.boards.query import (
    BoardQuery,
    Period,
    QueryInvalid,
    TotalRow,
    compare_period,
    run_query,
)
from oc8.boards.records import write_batch
from tests.conftest import AppSessionFactory

_SPEC: dict[str, Any] = {
    "datasets": [
        {
            "key": "sales",
            "label": "Sales",
            "grain": "day",
            "fields": [
                {"key": "amount", "label": "Amount", "type": "money", "currency": "EUR"},
                {"key": "margin", "label": "Margin", "type": "percent"},
                {"key": "region", "label": "Region", "type": "category"},
            ],
        },
        {
            "key": "stock",
            "label": "Stock",
            "grain": "moment",
            "fields": [{"key": "units", "label": "Units", "type": "number"}],
        },
    ],
    "blocks": [{"type": "kpi", "dataset": "sales", "field": "amount"}],
}


def P(a: str, b: str) -> Period:
    return Period(from_=dt.date.fromisoformat(a), to=dt.date.fromisoformat(b))


def _t(n: int) -> dt.datetime:
    return dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC) + dt.timedelta(minutes=n)


def q(period: tuple[str, str] = ("2026-03-01", "2026-04-01"), **kw: Any) -> BoardQuery:
    args: dict[str, Any] = {
        "dataset": "sales",
        "fields": ["amount"],
        "period": P(*period),
    }
    args.update(kw)
    return BoardQuery(**args)


class _Env:
    def __init__(self, db: AsyncSession, tenant: uuid.UUID) -> None:
        self.db, self.tenant = db, tenant
        self.n = 0
        self.agent: m.Agent
        self.board: m.DataBoard

    async def seed(self) -> _Env:
        dept = m.Department(tenant_id=self.tenant, name="Finance", frame={})
        self.db.add(dept)
        await self.db.flush()
        self.agent = m.Agent(tenant_id=self.tenant, department_id=dept.id, name="Reporter")
        self.db.add(self.agent)
        await self.db.flush()
        self.board = m.DataBoard(
            tenant_id=self.tenant,
            department_id=dept.id,
            agent_id=self.agent.id,
            title="Revenue",
            spec=_SPEC,
        )
        self.db.add(self.board)
        await self.db.flush()
        return self

    async def put(
        self,
        as_of: str,
        rows: list[dict[str, object]],
        dataset: str = "sales",
        note: str | None = None,
    ) -> None:
        self.n += 1
        await write_batch(
            self.db,
            tenant_id=self.tenant,
            agent=self.agent,
            board_id=self.board.id,
            dataset=dataset,
            as_of=as_of,
            rows=rows,
            note=note,
            run_id=None,
            now=_t(self.n),
        )

    async def run(self, query: BoardQuery) -> Any:
        return await run_query(self.db, board=self.board, query=query)


async def _march(env: _Env) -> None:
    await env.put(
        "2026-03-01", [{"region": "north", "amount": 100}, {"region": "south", "amount": 50}]
    )
    await env.put("2026-03-01", [{"region": "north", "amount": 120}])


async def test_latest_batch_replaces_the_whole_period(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await _march(env)
        r = await env.run(q())
        assert r.totals == [TotalRow(group=None, values={"amount": 120.0})]
        assert len(r.series) == 1 and r.series[0].as_of == "2026-03-01"


async def test_first_reported(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await _march(env)
        r = await env.run(q(reported="first"))
        assert r.totals == [TotalRow(group=None, values={"amount": 150.0})]


async def test_previous_period_change(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await _march(env)
        await env.put("2026-02-01", [{"region": "north", "amount": 100}])
        r = await env.run(q(compare="previous_period"))
        assert r.compare.period == Period(from_=dt.date(2026, 2, 1), to=dt.date(2026, 3, 1))
        assert r.compare.totals[0].values == {"amount": 100.0}
        assert r.compare.change[0].values == {"amount": pytest.approx(0.2)}


async def test_change_is_null_without_a_previous_value(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await _march(env)
        r = await env.run(q(compare="previous_period"))
        assert r.compare.totals == [TotalRow(group=None, values={"amount": None})]
        assert r.compare.change[0].values == {"amount": None}


async def test_percent_is_averaged(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await env.put("2026-03-01", [{"margin": 10}])
        await env.put("2026-03-02", [{"margin": 20}])
        r = await env.run(q(fields=["margin"]))
        assert r.totals[0].values == {"margin": 15.0}


async def test_moment_totals_use_the_latest_snapshot(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await env.put("2026-03-10T08:00:00", [{"units": 5}], dataset="stock")
        await env.put("2026-03-20T08:00:00", [{"units": 7}], dataset="stock")
        r = await env.run(q(dataset="stock", fields=["units"]))
        assert r.totals[0].values == {"units": 7.0}
        assert len(r.series) == 2
        assert r.series[0].as_of.startswith("2026-03-10T08:00")
        monthly = await env.run(q(dataset="stock", fields=["units"], bucket="month"))
        assert [p.values for p in monthly.series] == [{"units": 7.0}]


async def test_category_filter_and_group_by(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await _march(env)
        r = await env.run(q(filters={"region": ["north"]}))
        assert r.totals[0].values == {"amount": 120.0}
        none = await env.run(q(filters={"region": ["south"]}))
        assert none.totals[0].values == {"amount": None}
        g = await env.run(q(group_by="region"))
        assert [t.group for t in g.totals] == ["north"]
        assert [(p.group, p.values) for p in g.series] == [("north", {"amount": 120.0})]


async def test_series_buckets_by_month(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        for i in range(40):
            day = dt.date(2026, 2, 1) + dt.timedelta(days=i)
            await env.put(day.isoformat(), [{"amount": 1}])
        r = await env.run(q(period=("2026-02-01", "2026-04-01"), bucket="month"))
        assert [p.as_of for p in r.series] == ["2026-02-01", "2026-03-01"]
        assert [p.values["amount"] for p in r.series] == [28.0, 12.0]
        assert r.totals[0].values == {"amount": 40.0}


async def test_missing_field_in_old_records_is_null(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await env.put("2026-02-10", [{"amount": 5}])
        r = await env.run(q(period=("2026-02-01", "2026-03-01"), fields=["amount", "margin"]))
        assert r.totals[0].values == {"amount": 5.0, "margin": None}


async def test_rows_mode_sorts_and_pages(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await env.put(
            "2026-03-01",
            [
                {"region": "a", "amount": 100},
                {"region": "b", "amount": 120},
                {"region": "c", "amount": 90},
            ],
        )
        r = await env.run(
            q(
                mode="rows",
                fields=["amount", "region"],
                sort={"field": "amount", "dir": "desc"},
                page={"limit": 2},
            )
        )
        assert len(r.rows) == 2 and r.total_rows == 3
        assert r.rows[0] == {"as_of": "2026-03-01T00:00:00+00:00", "amount": 120, "region": "b"}
        assert r.rows[1]["amount"] == 100
        page2 = await env.run(
            q(
                mode="rows",
                fields=["amount"],
                sort={"field": "amount", "dir": "desc"},
                page={"offset": 2, "limit": 2},
            )
        )
        assert [x["amount"] for x in page2.rows] == [90]


async def test_note_is_the_latest_in_period(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        await env.put("2026-03-01", [{"amount": 1}], note="first note")
        await env.put("2026-03-05", [{"amount": 1}], note="second note")
        await env.put("2026-03-09", [{"amount": 1}], note=None)
        r = await env.run(q())
        assert r.note is not None
        assert r.note.text == "second note"
        assert r.note.as_of == dt.datetime(2026, 3, 5, tzinfo=dt.UTC)
        assert r.note.recorded_at == _t(2)
        empty = await env.run(q(period=("2026-05-01", "2026-06-01")))
        assert empty.note is None


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        ({"fields": ["nope"]}, "unknown_field"),
        ({"fields": ["region"]}, "not_numeric"),
        ({"filters": {"amount": ["1"]}}, "filter_not_category"),
        ({"group_by": "amount"}, "group_by_not_category"),
        ({"sort": {"field": "margin", "dir": "asc"}}, "sort_not_requested"),
        ({"dataset": "nope"}, "unknown_dataset"),
    ],
)
async def test_invalid_queries_are_refused(
    app_session: AppSessionFactory, bad: dict[str, Any], code: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        with pytest.raises(QueryInvalid) as exc:
            await env.run(q(**bad))
        assert exc.value.code == code


async def test_an_unparseable_spec_is_unknown_dataset(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        env = await _Env(db, tenant).seed()
        env.board.spec = {"datasets": "garbage"}
        with pytest.raises(QueryInvalid) as exc:
            await env.run(q())
        assert exc.value.code == "unknown_dataset"


def test_compare_period_rules() -> None:
    assert compare_period(P("2026-11-01", "2026-12-01"), "previous_period") == P(
        "2026-10-01", "2026-11-01"
    )
    assert compare_period(P("2025-12-01", "2026-12-01"), "previous_period") == P(
        "2024-12-01", "2025-12-01"
    )
    assert compare_period(P("2026-01-01", "2026-11-16"), "previous_period") == P(
        "2025-02-16", "2026-01-01"
    )
    assert compare_period(P("2026-11-01", "2026-12-01"), "same_period_last_year") == P(
        "2025-11-01", "2025-12-01"
    )
    assert compare_period(P("2024-02-29", "2024-03-01"), "same_period_last_year") == P(
        "2023-02-28", "2023-03-01"
    )


def test_period_validation() -> None:
    with pytest.raises(ValueError):
        P("2026-03-01", "2026-03-01")
    with pytest.raises(ValueError):
        P("2000-01-01", "2026-01-01")
    parsed = BoardQuery.model_validate(
        {
            "dataset": "sales",
            "fields": ["amount"],
            "period": {"from": "2026-03-01", "to": "2026-04-01"},
            "groupBy": "region",
        }
    )
    assert parsed.group_by == "region"
