"""Aggregate a board's append-only records in SQL.

Per `(dataset, as_of)` only one batch counts: the latest by default, the earliest
for `reported="first"`. Every JSON key used in SQL is checked against the dataset
spec first and travels as a bind parameter, never as SQL text.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator
from sqlalchemy import ColumnElement, Numeric, Select, Text, cast, func, null, select
from sqlalchemy.ext.asyncio import AsyncSession

import oc8.models as m
from oc8.boards.spec import NUMERIC_TYPES, BoardSpecInvalid, DatasetSpec, parse_board_spec
from oc8.schemas.base import CamelModel

MAX_PERIOD_DAYS = 7320
_KEY_ECHO_MAX = 40
#: Filter bounds: keys per query, values per key, characters per value.
MAX_FILTER_KEYS = 10
MAX_FILTER_VALUES = 200
MAX_FILTER_VALUE_LENGTH = 200

_FilterValues = Annotated[
    list[Annotated[str, Field(max_length=MAX_FILTER_VALUE_LENGTH)]],
    Field(max_length=MAX_FILTER_VALUES),
]


class QueryInvalid(ValueError):
    """The query does not fit the board. `code` is stable; messages carry no values."""

    def __init__(self, code: str, key: str | None = None) -> None:
        shown = key if key is not None and len(key) <= _KEY_ECHO_MAX else None
        super().__init__(f"{code}: {shown}" if shown else code)
        self.code = code


class Period(CamelModel):
    from_: dt.date = Field(alias="from")
    to: dt.date

    @model_validator(mode="after")
    def _check(self) -> Period:
        if self.to <= self.from_:
            raise ValueError("to must be after from")
        if (self.to - self.from_).days > MAX_PERIOD_DAYS:
            raise ValueError("period is too long")
        return self


class Page(CamelModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class Sort(CamelModel):
    field: str
    dir: Literal["asc", "desc"] = "asc"


class BoardQuery(CamelModel):
    dataset: str
    fields: list[str] = Field(min_length=1, max_length=20)
    period: Period
    compare: Literal["none", "previous_period", "same_period_last_year"] = "none"
    filters: dict[str, _FilterValues] = Field(default_factory=dict, max_length=MAX_FILTER_KEYS)
    group_by: str | None = None
    mode: Literal["aggregate", "rows"] = "aggregate"
    reported: Literal["latest", "first"] = "latest"
    bucket: Literal["grain", "week", "month", "year"] = "grain"
    page: Page = Field(default_factory=Page)
    sort: Sort | None = None


class SeriesPoint(CamelModel):
    as_of: str
    group: str | None = None
    values: dict[str, float | None]


class TotalRow(CamelModel):
    group: str | None = None
    values: dict[str, float | None]


class NoteOut(CamelModel):
    text: str
    as_of: dt.datetime
    recorded_at: dt.datetime


class CompareResult(CamelModel):
    period: Period
    series: list[SeriesPoint]
    totals: list[TotalRow]
    change: list[TotalRow]


class QueryResult(CamelModel):
    series: list[SeriesPoint] = Field(default_factory=list)
    totals: list[TotalRow] = Field(default_factory=list)
    compare: CompareResult | None = None
    rows: list[dict[str, object]] | None = None
    total_rows: int | None = None
    note: NoteOut | None = None


def _minus_years(d: dt.date, years: int) -> dt.date:
    year = d.year - years
    return d.replace(year=year, day=min(d.day, calendar.monthrange(year, d.month)[1]))


def _minus_months(d: dt.date, months: int) -> dt.date:
    index = d.year * 12 + (d.month - 1) - months
    return dt.date(index // 12, index % 12 + 1, 1)


def compare_period(period: Period, kind: str) -> Period:
    """The period `kind` compares `period` with. One that would start before the
    first representable date (`date.min`) is `invalid_period`, never a crash."""
    try:
        return _compare_period(period, kind)
    except (ValueError, OverflowError):
        raise QueryInvalid("invalid_period") from None


def _compare_period(period: Period, kind: str) -> Period:
    if kind == "same_period_last_year":
        return Period(from_=_minus_years(period.from_, 1), to=_minus_years(period.to, 1))
    if period.from_.day == 1 and period.to.day == 1:
        months = (period.to.year - period.from_.year) * 12 + period.to.month - period.from_.month
        return Period(from_=_minus_months(period.from_, months), to=period.from_)
    return Period(from_=period.from_ - (period.to - period.from_), to=period.from_)


def _midnight(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC)


def _check(ds: DatasetSpec, query: BoardQuery) -> None:
    for key in query.fields:
        f = ds.field(key)
        if f is None:
            raise QueryInvalid("unknown_field", key)
        if query.mode == "aggregate" and f.type not in NUMERIC_TYPES:
            raise QueryInvalid("not_numeric", key)
    for key in query.filters:
        f = ds.field(key)
        if f is None:
            raise QueryInvalid("unknown_field", key)
        if f.type != "category":
            raise QueryInvalid("filter_not_category", key)
    if query.group_by is not None:
        g = ds.field(query.group_by)
        if g is None:
            raise QueryInvalid("unknown_field", query.group_by)
        if g.type != "category":
            raise QueryInvalid("group_by_not_category", query.group_by)
    if query.sort is not None and query.sort.field != "as_of":
        if query.sort.field not in query.fields:
            raise QueryInvalid("sort_not_requested", query.sort.field)


def _chosen(board: m.DataBoard, query: BoardQuery, period: Period) -> Any:
    """The effective batches: one per `as_of` in the period."""
    b = m.DataBoardBatch
    first = query.reported == "first"
    return (
        select(b.id, b.as_of, b.recorded_at, b.note)
        .distinct(b.as_of)
        .where(
            b.board_id == board.id,
            b.dataset == query.dataset,
            b.as_of >= _midnight(period.from_),
            b.as_of < _midnight(period.to),
        )
        .order_by(
            b.as_of,
            b.recorded_at.asc() if first else b.recorded_at.desc(),
            b.id.asc() if first else b.id.desc(),
        )
        .cte("chosen")
    )


def _filters(query: BoardQuery) -> list[ColumnElement[bool]]:
    r = m.DataBoardRecord
    return [r.values[key].astext.in_(allowed) for key, allowed in query.filters.items()]


def _numeric(key: str) -> ColumnElement[Any]:
    col: ColumnElement[Any] = m.DataBoardRecord.values[key].astext.cast(Numeric)
    return col


def _num(value: object) -> float | None:
    return None if value is None else float(str(value))


async def _aggregate(
    db: AsyncSession,
    board: m.DataBoard,
    ds: DatasetSpec,
    query: BoardQuery,
    period: Period,
    *,
    bucketed: bool,
) -> list[tuple[str | None, str | None, dict[str, float | None]]]:
    r = m.DataBoardRecord
    chosen = _chosen(board, query, period)
    bucket_expr: ColumnElement[Any] | None = None
    if bucketed:
        if query.bucket == "grain":
            bucket_expr = chosen.c.as_of
        else:
            bucket_expr = func.date_trunc(query.bucket, func.timezone("UTC", chosen.c.as_of)).cast(
                Text
            )
    window = (
        func.max(chosen.c.as_of).over(partition_by=bucket_expr)
        if bucket_expr is not None
        else func.max(chosen.c.as_of).over()
    )
    eff = select(
        chosen.c.id.label("id"),
        chosen.c.as_of.label("as_of"),
        (bucket_expr if bucket_expr is not None else chosen.c.as_of).label("bucket"),
        window.label("latest"),
    ).cte("eff")

    cols: list[ColumnElement[Any]] = [eff.c.bucket, _group_col(query)]
    cols += [_numeric(key).label(f"f{i}") for i, key in enumerate(query.fields)]
    inner = select(*cols).select_from(r).join(eff, r.batch_id == eff.c.id)
    if ds.grain == "moment":
        inner = inner.where(eff.c.as_of == eff.c.latest)
    for pred in _filters(query):
        inner = inner.where(pred)
    sub = inner.subquery("rec")

    aggs: list[ColumnElement[Any]] = []
    for i, key in enumerate(query.fields):
        f = ds.field(key)
        fn = func.avg if f is not None and f.type == "percent" else func.sum
        aggs.append(fn(sub.c[f"f{i}"]).label(f"a{i}"))
    group_cols: list[ColumnElement[Any]] = [sub.c.grp]
    if bucketed:
        group_cols.insert(0, sub.c.bucket)
    stmt: Select[Any] = select(*group_cols, *aggs).group_by(*group_cols)
    stmt = stmt.order_by(*group_cols)
    out: list[tuple[str | None, str | None, dict[str, float | None]]] = []
    for row in (await db.execute(stmt)).all():
        if bucketed:
            bucket, grp, *vals = row
        else:
            bucket = None
            grp, *vals = row
        values = {key: _num(v) for key, v in zip(query.fields, vals, strict=True)}
        out.append((_bucket_label(bucket, ds, query), grp, values))
    return out


def _group_col(query: BoardQuery) -> ColumnElement[Any]:
    if query.group_by is None:
        return cast(null(), Text).label("grp")
    grp: ColumnElement[Any] = m.DataBoardRecord.values[query.group_by].astext.label("grp")
    return grp


def _bucket_label(bucket: object, ds: DatasetSpec, query: BoardQuery) -> str | None:
    if bucket is None:
        return None
    if isinstance(bucket, dt.datetime):
        if ds.grain == "moment":
            return bucket.astimezone(dt.UTC).isoformat()
        return bucket.astimezone(dt.UTC).date().isoformat()
    return str(bucket)[:10]


def _totals(
    rows: list[tuple[str | None, str | None, dict[str, float | None]]],
    query: BoardQuery,
) -> list[TotalRow]:
    out = [TotalRow(group=grp, values=values) for _, grp, values in rows]
    if not out and query.group_by is None:
        out = [TotalRow(group=None, values=dict.fromkeys(query.fields))]
    return out


def _change(current: list[TotalRow], previous: list[TotalRow]) -> list[TotalRow]:
    prev = {t.group: t.values for t in previous}
    out: list[TotalRow] = []
    for t in current:
        before = prev.get(t.group, {})
        values: dict[str, float | None] = {}
        for key, cur in t.values.items():
            p = before.get(key)
            values[key] = None if cur is None or not p else (cur - p) / abs(p)
        out.append(TotalRow(group=t.group, values=values))
    return out


async def _run_aggregate(
    db: AsyncSession,
    board: m.DataBoard,
    ds: DatasetSpec,
    query: BoardQuery,
    period: Period,
) -> tuple[list[SeriesPoint], list[TotalRow]]:
    series_rows = await _aggregate(db, board, ds, query, period, bucketed=True)
    total_rows = await _aggregate(db, board, ds, query, period, bucketed=False)
    series = [
        SeriesPoint(as_of=bucket or "", group=grp, values=values)
        for bucket, grp, values in series_rows
    ]
    return series, _totals(total_rows, query)


async def _rows(
    db: AsyncSession,
    board: m.DataBoard,
    ds: DatasetSpec,
    query: BoardQuery,
) -> tuple[list[dict[str, object]], int]:
    r = m.DataBoardRecord
    chosen = _chosen(board, query, query.period)
    base = select(r.as_of, r.values).join(chosen, r.batch_id == chosen.c.id)
    for pred in _filters(query):
        base = base.where(pred)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))

    sort = query.sort or Sort(field="as_of", dir="desc")
    key: ColumnElement[Any]
    if sort.field == "as_of":
        key = r.as_of.expression
    else:
        f = ds.field(sort.field)
        key = (
            _numeric(sort.field)
            if f is not None and f.type in NUMERIC_TYPES
            else (r.values[sort.field].astext)
        )
    ordered = key.asc().nulls_last() if sort.dir == "asc" else key.desc().nulls_last()
    stmt = base.order_by(ordered, r.id).offset(query.page.offset).limit(query.page.limit)
    out: list[dict[str, object]] = []
    for as_of, values in (await db.execute(stmt)).all():
        row: dict[str, object] = {"as_of": as_of.astimezone(dt.UTC).isoformat()}
        for fkey in query.fields:
            row[fkey] = values.get(fkey)
        out.append(row)
    return out, int(total or 0)


async def _note(db: AsyncSession, board: m.DataBoard, query: BoardQuery) -> NoteOut | None:
    chosen = _chosen(board, query, query.period)
    row = (
        await db.execute(
            select(chosen.c.note, chosen.c.as_of, chosen.c.recorded_at)
            .where(chosen.c.note.is_not(None))
            .order_by(chosen.c.as_of.desc(), chosen.c.recorded_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return NoteOut(text=row[0], as_of=row[1], recorded_at=row[2])


async def run_query(db: AsyncSession, *, board: m.DataBoard, query: BoardQuery) -> QueryResult:
    try:
        spec = parse_board_spec(board.spec)
    except BoardSpecInvalid:
        raise QueryInvalid("unknown_dataset") from None
    ds = next((d for d in spec.datasets if d.key == query.dataset), None)
    if ds is None:
        raise QueryInvalid("unknown_dataset", query.dataset)
    _check(ds, query)

    result = QueryResult(note=await _note(db, board, query))
    if query.mode == "rows":
        result.rows, result.total_rows = await _rows(db, board, ds, query)
        return result

    result.series, result.totals = await _run_aggregate(db, board, ds, query, query.period)
    if query.compare != "none":
        prior = compare_period(query.period, query.compare)
        series, totals = await _run_aggregate(db, board, ds, query, prior)
        result.compare = CompareResult(
            period=prior,
            series=series,
            totals=totals,
            change=_change(result.totals, totals),
        )
    return result
