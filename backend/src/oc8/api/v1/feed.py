"""Activity feed, approvals inbox, and usage rollups (read)."""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, and_, func, literal_column, or_, select

from oc8 import models as m
from oc8.agents.repo import resolve_run_reach, restrict_to_run_reach
from oc8.api.deps import CurrentPrincipal, DbSession, require_departmental, require_permission
from oc8.api.v1._export_utils import csv_safe as _csv_safe
from oc8.api.v1._export_utils import parse_date_bound as _parse_bound
from oc8.api.v1._serializers import activity_to_dto, approval_to_dto, resolve_approval_names
from oc8.approvals.repo import DEFAULT_LIMIT, visible_approvals
from oc8.audit.integrity import (
    BATCH as BATCH,  # re-exported: tests monkeypatch this name, same as audit.py
)
from oc8.authz.permissions import BUDGET, RUN, VIEW, perm
from oc8.authz.scope import APPROVAL_VIEW, HumanActor
from oc8.metering.budget import month_start
from oc8.metering.pricing import active_price_rows, cost_micros_from_price, price_as_of
from oc8.schemas.dto import (
    ActivityDTO,
    ApprovalDTO,
    PrincipalUsageDTO,
    TokenUsageExportRowDTO,
    UsageSeriesDTO,
    UsageTimeseriesDTO,
)
from oc8.triggers.zones import organisation_timezone

router = APIRouter()


@router.get("/activity", response_model=list[ActivityDTO])
async def list_activity(
    request: Request,
    db: DbSession,
    # A route parameter, not `dependencies=[...]`: the body needs the actor's
    # scope to narrow the feed to the agents the caller may see.
    actor: Annotated[HumanActor, Depends(require_departmental(perm(RUN, VIEW)))],
    limit: int = 50,
    agent_id: uuid.UUID | None = Query(default=None, alias="agentId"),
    before: uuid.UUID | None = None,
) -> list[ActivityDTO]:
    """Newest first, optionally for one agent, optionally starting after a row.

    `agent_id` is served here rather than filtered in the browser: a global
    page of 50 can contain nothing at all from the agent whose screen you are
    on, so its feed looked empty while its history sat just past the cut.

    `before` takes the id of the last row you were given, not an offset. Ids are
    time-ordered (uuid7) and rows only ever arrive at the newest end, so paging
    by id cannot skip or repeat a row the way OFFSET does when the feed grows
    between two requests.

    Narrowed to the agents whose runs the caller may see (`resolve_run_reach`,
    the rule every run route applies), in the statement and before the limit.
    An event naming no agent is kept only for a tenant-wide reader: there is
    no agent whose department could say a departmental caller may see it.
    """
    limit = max(1, min(limit, 200))
    reach = await resolve_run_reach(request, db, actor, perm(RUN, VIEW))
    query = restrict_to_run_reach(select(m.ActivityEvent), m.ActivityEvent.agent_id, reach)
    if agent_id is not None:
        query = query.where(m.ActivityEvent.agent_id == agent_id)
    if before is not None:
        query = query.where(m.ActivityEvent.id < before)
    rows = (
        (
            await db.execute(
                # ts DESC is what a reader means by "newest"; id breaks the tie,
                # and is also what `before` pages on, so the two must agree.
                query.order_by(m.ActivityEvent.ts.desc(), m.ActivityEvent.id.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [activity_to_dto(e) for e in rows]


@router.get("/approvals", response_model=list[ApprovalDTO])
async def list_approvals(
    db: DbSession,
    # A ROUTE PARAMETER and not `dependencies=[...]`: the body needs the actor,
    # and a dependency in the decorator list is resolved and then thrown away.
    actor: Annotated[HumanActor, Depends(require_departmental(APPROVAL_VIEW))],
    status: str = "pending",
    department_id: uuid.UUID | None = Query(default=None, alias="departmentId"),
    limit: int = DEFAULT_LIMIT,
) -> list[ApprovalDTO]:
    """The approvals this caller may see -- not the tenant's.

    Until this slice the query was `WHERE status = :status` and nothing else, so
    a company that gave its Head of Sales an account gave him every pending
    approval in it: Engineering's production access request, its titles and its
    amounts. They are not filtered out in the browser now; they are never
    fetched.

    `departmentId` INTERSECTS the scope and never widens it -- it is the picker
    an admin gets over departments he can already see, so asking for somebody
    else's returns `[]` rather than 403 (a 403 would answer "does that
    department exist"). `limit` is clamped in the repository.

    A seat-holder whose queue is quiet gets 200 and `[]`; somebody with no seat
    anywhere is refused at the gate with a sentence telling him to ask an
    administrator. Those two states are the same blank screen today, and "the
    system is broken" must not look like "nobody has added me yet" (§7).
    """
    rows = await visible_approvals(
        db, actor=actor, status=status, department_id=department_id, limit=limit
    )
    # Four batched lookups for the whole page, not four per row: the detail pane
    # names the agent, the department and the task, and resolving those while
    # rendering is how a queue of a hundred becomes four hundred and one requests.
    names = await resolve_approval_names(db, rows)
    return [approval_to_dto(a, names) for a in rows]


@router.get(
    "/usage",
    response_model=list[PrincipalUsageDTO],
    dependencies=[Depends(require_permission(perm(BUDGET, VIEW)))],
)
async def usage(
    db: DbSession,
    principal: CurrentPrincipal,
    group_by: str = "department",
    period: Literal["month", "all"] = "month",
) -> list[PrincipalUsageDTO]:
    """Token and cost totals per department or agent. `month` is the current
    calendar month on the organisation's clock, `all` everything recorded."""
    col = (
        m.TokenUsageRecord.department_id
        if group_by == "department"
        else m.TokenUsageRecord.agent_id
    )
    day = func.date_trunc("day", m.TokenUsageRecord.ts)
    # Explicit, not left to RLS alone: a session that is not tenant-bound (the
    # schema owner, a misconfigured role) would otherwise sum every tenant.
    filters = [m.TokenUsageRecord.tenant_id == principal.tenant_id]
    if period == "month":
        zone = await organisation_timezone(db, principal.tenant_id)
        filters.append(m.TokenUsageRecord.ts >= month_start(zone))
    rows = (
        await db.execute(
            select(
                col,
                day,
                m.TokenUsageRecord.provider,
                m.TokenUsageRecord.model,
                func.coalesce(func.sum(m.TokenUsageRecord.tokens_in), 0),
                func.coalesce(func.sum(m.TokenUsageRecord.tokens_out), 0),
                func.coalesce(func.sum(m.TokenUsageRecord.saved_tokens_in), 0),
                func.coalesce(func.sum(m.TokenUsageRecord.saved_tokens_out), 0),
                func.coalesce(func.sum(m.TokenUsageRecord.cache_read_tokens), 0),
                func.coalesce(func.sum(m.TokenUsageRecord.cache_write_tokens), 0),
            )
            .where(*filters)
            .group_by(col, day, m.TokenUsageRecord.provider, m.TokenUsageRecord.model)
        )
    ).all()

    now = datetime.now(UTC)
    price_rows = await active_price_rows(db, as_of=now)

    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": 0,
            "saved_tokens_in": 0,
            "saved_tokens_out": 0,
            "saved_cost": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    for (
        group,
        bucket_day,
        provider,
        model,
        tokens_in,
        tokens_out,
        saved_in,
        saved_out,
        cache_read,
        cache_write,
    ) in rows:
        key = str(group) if group else "unassigned"
        # `bucket_day` is the midnight start of this bucket's day, but a price
        # row's effective_from is a real wall-clock timestamp -- a price edited
        # at, say, 14:00 today has an effective_from *after* today's midnight,
        # so comparing against the bucket's start would make it (and any price
        # from "today") never match its own day's usage. Compare against the
        # end of the bucket's day instead: any price effective at any point
        # during that day applies to that day's report, matching the day-level
        # granularity this endpoint already chose.
        price = price_as_of(price_rows, provider, model, bucket_day + timedelta(days=1))
        cost = (
            cost_micros_from_price(
                price,
                int(tokens_in),
                int(tokens_out),
                cache_read_tokens=int(cache_read),
                cache_write_tokens=int(cache_write),
                model=model,
            )
            if price
            else 0
        )
        saved_cost = cost_micros_from_price(price, int(saved_in), int(saved_out)) if price else 0
        totals[key]["tokens_in"] += int(tokens_in)
        totals[key]["tokens_out"] += int(tokens_out)
        totals[key]["cost"] += cost
        totals[key]["saved_tokens_in"] += int(saved_in)
        totals[key]["saved_tokens_out"] += int(saved_out)
        totals[key]["saved_cost"] += saved_cost
        totals[key]["cache_read"] += int(cache_read)
        totals[key]["cache_write"] += int(cache_write)

    return [
        PrincipalUsageDTO(
            group=group,
            tokens_in=v["tokens_in"],
            tokens_out=v["tokens_out"],
            provider_cost_micros=v["cost"],
            saved_tokens_in=v["saved_tokens_in"],
            saved_tokens_out=v["saved_tokens_out"],
            saved_cost_micros=v["saved_cost"],
            cache_read_tokens=v["cache_read"],
            cache_write_tokens=v["cache_write"],
        )
        for group, v in totals.items()
    ]


UsageRange = Literal["24h", "7d", "30d"]

#: range -> (bucket unit, number of buckets). The last bucket is the one "now"
#: falls in, so a range always ends with the hour or day still filling up.
_USAGE_RANGES: dict[str, tuple[Literal["hour", "day"], int]] = {
    "24h": ("hour", 24),
    "7d": ("hour", 168),
    "30d": ("day", 30),
}

#: Members with their own series; everyone below them is summed into "others"
#: so a large organisation still gets a chart a person can read.
_USAGE_TOP_MEMBERS = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _usage_buckets(
    unit: Literal["hour", "day"], count: int, zone: ZoneInfo, now: datetime
) -> list[datetime]:
    """Bucket starts on the organisation's clock, oldest first, ending with the
    bucket that holds `now`.

    The range reaches back `count - 1` units of wall-clock time, so a range
    across a clock change holds one hourly bucket more or fewer than `count`.
    Hourly buckets are stepped in UTC, not on the wall clock: the autumn
    change has two different hours both labelled 02:00, and stepping in real
    time is what keeps them two buckets (each carrying its own offset)
    instead of one bucket counted twice or one hour dropped.
    """
    local_now = now.astimezone(zone)
    if unit == "day":
        today = local_now.date()
        return [
            datetime.combine(today - timedelta(days=i), datetime.min.time(), tzinfo=zone)
            for i in range(count - 1, -1, -1)
        ]
    end = local_now.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
    wall_start = local_now.replace(minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(
        hours=count - 1
    )
    start = wall_start.replace(tzinfo=zone).astimezone(UTC)
    buckets: list[datetime] = []
    t = start
    while t <= end:
        buckets.append(t.astimezone(zone))
        t += timedelta(hours=1)
    return buckets


@router.get(
    "/usage/timeseries",
    response_model=UsageTimeseriesDTO,
    dependencies=[Depends(require_permission(perm(BUDGET, VIEW)))],
)
async def usage_timeseries(
    db: DbSession,
    principal: CurrentPrincipal,
    range_: Annotated[UsageRange, Query(alias="range")] = "7d",
) -> UsageTimeseriesDTO:
    """Tokens (in + out) per person over the last 24 hours, 7 days or 30 days,
    bucketed hourly (24h, 7d) or daily (30d) on the organisation's clock.

    The ten members with the most tokens in the range get a series each, the
    rest are summed into `others`; usage no person is attached to (history
    from before member attribution) is the `none` series. A range nobody used
    still returns its full bucket axis with no series, so the chart draws an
    empty grid instead of collapsing.
    """
    unit, count = _USAGE_RANGES[range_]
    zone_name = await organisation_timezone(db, principal.tenant_id)
    zone = ZoneInfo(zone_name)
    buckets = _usage_buckets(unit, count, zone, _now())
    rec = m.TokenUsageRecord
    local_ts = func.timezone(zone_name, rec.ts)
    local_bucket = func.date_trunc(unit, local_ts)
    # The zone's offset at the record's instant. An hour bucket shares it with
    # its records, and it is what tells the two 02:00 hours of an autumn clock
    # change apart -- the local wall time alone names them identically.
    offset = local_ts - func.timezone("UTC", rec.ts)
    group_cols: list[Any] = [rec.member_id, local_bucket]
    if unit == "hour":
        group_cols.append(offset)
    rows = (
        await db.execute(
            select(*group_cols, func.sum(rec.tokens_in + rec.tokens_out))
            # Explicit, not left to RLS alone (same reason as `GET /usage`).
            .where(rec.tenant_id == principal.tenant_id, rec.ts >= buckets[0])
            # By position: the zone is a bound parameter, and Postgres does not
            # treat `timezone($1, ts)` in the SELECT and `timezone($4, ts)` in
            # the GROUP BY as the same expression.
            .group_by(*(literal_column(str(i + 1)) for i in range(len(group_cols))))
        )
    ).all()

    index: dict[Any, int] = (
        {b.astimezone(UTC): i for i, b in enumerate(buckets)}
        if unit == "hour"
        else {b.date(): i for i, b in enumerate(buckets)}
    )
    points: dict[uuid.UUID | None, list[int]] = defaultdict(lambda: [0] * len(buckets))
    for row in rows:
        member_id, bucket_start, tokens = row[0], row[1], row[-1]
        if unit == "hour":
            key: Any = (bucket_start - row[2]).replace(tzinfo=UTC)
        else:
            key = bucket_start.date()
        # No bucket: a record stamped after the current bucket (clock skew) is dropped.
        i = index.get(key)
        if i is not None:
            points[member_id][i] += int(tokens or 0)

    totals = {member: sum(p) for member, p in points.items() if sum(p) > 0}
    ranked = sorted(
        (member for member in totals if member is not None),
        key=lambda member: (-totals[member], str(member)),
    )
    top, rest = ranked[:_USAGE_TOP_MEMBERS], ranked[_USAGE_TOP_MEMBERS:]
    names: dict[uuid.UUID, str] = {}
    if top:
        found = await db.execute(
            select(m.OrgMember.id, m.OrgMember.display_name).where(m.OrgMember.id.in_(top))
        )
        names = {member: name for member, name in found.all() if name}

    series = [
        UsageSeriesDTO(
            key=str(member),
            member_id=member,
            name=names.get(member),
            total=totals[member],
            points=points[member],
        )
        for member in top
    ]
    if rest:
        others = [sum(column) for column in zip(*(points[member] for member in rest), strict=True)]
        series.append(
            UsageSeriesDTO(
                key="others", member_id=None, name=None, total=sum(others), points=others
            )
        )
    if None in totals:
        series.append(
            UsageSeriesDTO(
                key="none", member_id=None, name=None, total=totals[None], points=points[None]
            )
        )
    return UsageTimeseriesDTO(
        range=range_, bucket=unit, timezone=zone_name, buckets=buckets, series=series
    )


# Column names are stable; new columns may be added. They are the
# TokenUsageExportRowDTO camelCase aliases (matching the JSONL branch below) so
# a CSV export and a JSONL export of the same records key identically for a
# downstream ETL.
_USAGE_EXPORT_COLUMNS = [
    "ts",
    "agentId",
    "agentName",
    "departmentId",
    "departmentName",
    "memberId",
    "memberName",
    "model",
    "provider",
    "tokensIn",
    "tokensOut",
    "cacheHit",
    "savedTokensIn",
    "savedTokensOut",
    "platformUnits",
    "costMicros",
    "skillId",
    "requestId",
]


def _usage_row(dto: TokenUsageExportRowDTO) -> list[str]:
    # Single by_alias dump, indexed by the same alias keys used for the header,
    # so header and values cannot drift apart.
    d = dto.model_dump(by_alias=True)
    out: list[str] = []
    for col in _USAGE_EXPORT_COLUMNS:
        val = d[col]
        cell = "" if val is None else str(val)
        out.append(_csv_safe(cell))
    return out


def _usage_filtered(
    stmt: Select[tuple[Any, ...]],
    *,
    agent_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    model: str | None,
    provider: str | None,
    from_ts: datetime | None,
    to_ts: datetime | None,
) -> Select[tuple[Any, ...]]:
    if agent_id is not None:
        stmt = stmt.where(m.TokenUsageRecord.agent_id == agent_id)
    if department_id is not None:
        stmt = stmt.where(m.TokenUsageRecord.department_id == department_id)
    if model:
        stmt = stmt.where(m.TokenUsageRecord.model == model)
    if provider:
        stmt = stmt.where(m.TokenUsageRecord.provider == provider)
    if from_ts:
        stmt = stmt.where(m.TokenUsageRecord.ts >= from_ts)
    if to_ts:
        stmt = stmt.where(m.TokenUsageRecord.ts <= to_ts)
    return stmt


@router.get("/usage/export", dependencies=[Depends(require_permission(perm(BUDGET, VIEW)))])
async def export_usage(
    db: DbSession,
    format: Literal["csv", "jsonl"] = "csv",
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query(alias="to")] = None,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> StreamingResponse:
    """A one-row-per-request export of `token_usage_record`, in the same
    streaming CSV/JSONL shape as `GET /audit/export` -- reuses `budget:view`
    rather than adding a permission, since this exports exactly what
    `GET /usage` already returns to that permission, just unrolled to raw
    records instead of a per-day/per-group aggregate.

    Agent and department names are user-supplied and are the live CSV
    injection vector here (see `_csv_safe`).

    `costMicros` is computed at render time from the versioned `ModelPrice`
    table via `metering/pricing.py`'s `price_as_of` -- cost is deliberately
    not stored on `TokenUsageRecord`, and this export must not invent a
    second pricing path.

    `TokenUsageRecord` has no `seq`-style unique, monotonic identity column
    the way `AuditEvent` does, so paging on `ts` alone would repeat or skip
    rows whenever a batch boundary lands inside a group of identical
    timestamps (routine here -- many requests can be metered in the same
    instant). The cursor is therefore the composite `(ts, id)`: `id` is a
    uuid7 (time-ordered) and, combined with `ts`, is always unique.
    """
    from_ts = _parse_bound(from_, field="from", end_of_day=False)
    to_ts = _parse_bound(to, field="to", end_of_day=True)
    base = _usage_filtered(
        select(m.TokenUsageRecord, m.Agent.name, m.Department.name, m.OrgMember.display_name)
        .outerjoin(m.Agent, m.Agent.id == m.TokenUsageRecord.agent_id)
        .outerjoin(m.Department, m.Department.id == m.TokenUsageRecord.department_id)
        .outerjoin(m.OrgMember, m.OrgMember.id == m.TokenUsageRecord.member_id),
        agent_id=agent_id,
        department_id=department_id,
        model=model,
        provider=provider,
        from_ts=from_ts,
        to_ts=to_ts,
    )

    now = datetime.now(UTC)
    price_rows = await active_price_rows(db, as_of=now)

    # NOTE: this generator keeps using the request-scoped `db` (DbSession) after
    # the handler has returned the StreamingResponse. That's only safe because,
    # on this repo's installed FastAPI (0.139.0), dependency cleanup (closing
    # `db` and unsetting its RLS tenant GUC) runs only once the response body
    # generator is fully drained -- verified empirically. A future FastAPI
    # upgrade that reorders cleanup ahead of body draining would break this
    # export (session closed / wrong tenant mid-stream); re-check on upgrade.
    async def _stream() -> AsyncIterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if format == "csv":
            writer.writerow(_USAGE_EXPORT_COLUMNS)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
        cursor_ts: datetime | None = None
        cursor_id: uuid.UUID | None = None
        while True:
            stmt = base
            if cursor_ts is not None and cursor_id is not None:
                stmt = stmt.where(
                    or_(
                        m.TokenUsageRecord.ts > cursor_ts,
                        and_(
                            m.TokenUsageRecord.ts == cursor_ts,
                            m.TokenUsageRecord.id > cursor_id,
                        ),
                    )
                )
            rows = (
                await db.execute(
                    stmt.order_by(m.TokenUsageRecord.ts.asc(), m.TokenUsageRecord.id.asc()).limit(
                        BATCH
                    )
                )
            ).all()
            if not rows:
                return
            for rec, agent_name, department_name, member_name in rows:
                # Price at the record's OWN time, not today's -- editing a price
                # today must not change what a historical record's export shows
                # (same historical-accuracy contract as GET /usage).
                price = price_as_of(price_rows, rec.provider, rec.model, rec.ts)
                cost_micros = (
                    cost_micros_from_price(
                        price,
                        rec.tokens_in,
                        rec.tokens_out,
                        cache_read_tokens=rec.cache_read_tokens,
                        cache_write_tokens=rec.cache_write_tokens,
                        model=rec.model,
                    )
                    if price
                    else None
                )
                dto = TokenUsageExportRowDTO(
                    ts=rec.ts.isoformat(),
                    agent_id=str(rec.agent_id) if rec.agent_id else None,
                    agent_name=agent_name,
                    department_id=str(rec.department_id) if rec.department_id else None,
                    department_name=department_name,
                    member_id=str(rec.member_id) if rec.member_id else None,
                    member_name=member_name or None,
                    model=rec.model,
                    provider=rec.provider,
                    tokens_in=rec.tokens_in,
                    tokens_out=rec.tokens_out,
                    cache_hit=rec.cache_hit,
                    saved_tokens_in=rec.saved_tokens_in,
                    saved_tokens_out=rec.saved_tokens_out,
                    platform_units=rec.platform_units,
                    cost_micros=cost_micros,
                    skill_id=str(rec.skill_id) if rec.skill_id else None,
                    request_id=str(rec.request_id),
                )
                if format == "csv":
                    writer.writerow(_usage_row(dto))
                else:
                    buf.write(json.dumps(dto.model_dump(by_alias=True), default=str) + "\n")
                cursor_ts = rec.ts
                cursor_id = rec.id
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    media = "text/csv" if format == "csv" else "application/x-ndjson"
    return StreamingResponse(
        _stream(),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="usage-export.{format}"'},
    )
