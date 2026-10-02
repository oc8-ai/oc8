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

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, and_, func, or_, select

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental, require_permission
from oc8.api.v1._export_utils import csv_safe as _csv_safe
from oc8.api.v1._export_utils import parse_date_bound as _parse_bound
from oc8.api.v1._serializers import activity_to_dto, approval_to_dto, resolve_approval_names
from oc8.approvals.repo import DEFAULT_LIMIT, visible_approvals
from oc8.audit.integrity import (
    BATCH as BATCH,  # re-exported: tests monkeypatch this name, same as audit.py
)
from oc8.authz.permissions import BUDGET, RUN, VIEW, perm
from oc8.authz.scope import APPROVAL_VIEW, HumanActor
from oc8.metering.pricing import active_price_rows, cost_micros_from_price, price_as_of
from oc8.schemas.dto import ActivityDTO, ApprovalDTO, PrincipalUsageDTO, TokenUsageExportRowDTO

router = APIRouter()


@router.get(
    "/activity",
    response_model=list[ActivityDTO],
    dependencies=[Depends(require_permission(perm(RUN, VIEW)))],
)
async def list_activity(
    db: DbSession,
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
    """
    limit = max(1, min(limit, 200))
    query = select(m.ActivityEvent)
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
async def usage(db: DbSession, group_by: str = "department") -> list[PrincipalUsageDTO]:
    col = (
        m.TokenUsageRecord.department_id
        if group_by == "department"
        else m.TokenUsageRecord.agent_id
    )
    day = func.date_trunc("day", m.TokenUsageRecord.ts)
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
            ).group_by(col, day, m.TokenUsageRecord.provider, m.TokenUsageRecord.model)
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
        }
    )
    for group, bucket_day, provider, model, tokens_in, tokens_out, saved_in, saved_out in rows:
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
        cost = cost_micros_from_price(price, int(tokens_in), int(tokens_out)) if price else 0
        saved_cost = cost_micros_from_price(price, int(saved_in), int(saved_out)) if price else 0
        totals[key]["tokens_in"] += int(tokens_in)
        totals[key]["tokens_out"] += int(tokens_out)
        totals[key]["cost"] += cost
        totals[key]["saved_tokens_in"] += int(saved_in)
        totals[key]["saved_tokens_out"] += int(saved_out)
        totals[key]["saved_cost"] += saved_cost

    return [
        PrincipalUsageDTO(
            group=group,
            tokens_in=v["tokens_in"],
            tokens_out=v["tokens_out"],
            provider_cost_micros=v["cost"],
            saved_tokens_in=v["saved_tokens_in"],
            saved_tokens_out=v["saved_tokens_out"],
            saved_cost_micros=v["saved_cost"],
        )
        for group, v in totals.items()
    ]


# Column order is stable; column NAMES are the TokenUsageExportRowDTO camelCase
# aliases (matching the JSONL branch below) so a CSV export and a JSONL export
# of the same records key identically for a downstream ETL.
_USAGE_EXPORT_COLUMNS = [
    "ts",
    "agentId",
    "agentName",
    "departmentId",
    "departmentName",
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
        select(m.TokenUsageRecord, m.Agent.name, m.Department.name)
        .outerjoin(m.Agent, m.Agent.id == m.TokenUsageRecord.agent_id)
        .outerjoin(m.Department, m.Department.id == m.TokenUsageRecord.department_id),
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
            for rec, agent_name, department_name in rows:
                # Price at the record's OWN time, not today's -- editing a price
                # today must not change what a historical record's export shows
                # (same historical-accuracy contract as GET /usage).
                price = price_as_of(price_rows, rec.provider, rec.model, rec.ts)
                cost_micros = (
                    cost_micros_from_price(price, rec.tokens_in, rec.tokens_out) if price else None
                )
                dto = TokenUsageExportRowDTO(
                    ts=rec.ts.isoformat(),
                    agent_id=str(rec.agent_id) if rec.agent_id else None,
                    agent_name=agent_name,
                    department_id=str(rec.department_id) if rec.department_id else None,
                    department_name=department_name,
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
