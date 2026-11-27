"""Live KPI reads for one agent, one department, or the whole tenant -- see
backend/src/oc8/kpis/aggregate.py for how the numbers are computed.

The agent- and department-scoped routes are `require_departmental` -- READ
graduates for any live seat, mirroring `agents.py::get_agent` and
`departments.py::get_department` exactly, including their `visible_agent`/
`visible_department` narrow: the door only proves the caller holds
`agent:view`/`department:view` SOMEWHERE, so the row itself still has to be
proven visible before its numbers go out, or a Sales-seated caller could read
Engineering's KPIs by id even though `GET /agents/{id}` would 404 the same
request. Both 404 identically for "no such row" and "exists outside scope",
the same oracle guard those two reads already use.

`GET /kpis` is tenant-wide-only (`statistics:view`, gated by
`require_permission`, never `require_departmental`) -- see
`oc8.authz.permissions.STATISTICS` for why this is its own resource rather
than folded into AGENT/DEPARTMENT: it is the one endpoint that can span every
agent and department in the tenant in a single request.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import ColumnElement, Select, Text, Uuid, cast, func, literal_column, select

from oc8 import models as m
from oc8.agents.repo import visible_agent
from oc8.api.deps import CurrentPrincipal, DbSession, require_departmental, require_permission
from oc8.authz.authority import authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, DEPARTMENT, MANAGE, STATISTICS, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.departments.repo import visible_department
from oc8.kpis.aggregate import KPIResult, compute_kpis
from oc8.models.core import DEPARTMENT_KIND_STANDARD
from oc8.schemas.kpi import KPIDTO, FeedbackCountDTO, KPIGroupRowDTO

router = APIRouter()

_require_stats = require_permission(perm(STATISTICS, VIEW))

#: `group_by` values the tenant-wide endpoint accepts. `compute_kpis` itself
#: only names the vocabulary (`kpis.aggregate.GroupBy`); which values actually
#: exist is this endpoint's own concern, since grouping is performed here (see
#: `compute_kpis`'s own docstring on why).
_GROUP_BY_VALUES = frozenset({"agent", "department", "day", "week", "month"})

#: Caps how many buckets a single `groupBy=day|week|month` request may
#: produce. Mirrors `audit.py`'s own `MAX_LIMIT` convention for exactly this
#: class of problem: without a cap, `groupBy=day&dateFrom=1900-01-01` fires
#: `compute_kpis`'s six sub-queries sequentially for every one of tens of
#: thousands of buckets, turning one authenticated HTTP request into a huge
#: number of DB round trips. Rejected outright rather than silently
#: truncated -- a truncated series is a chart that LOOKS complete and is
#: quietly wrong, with nothing telling the caller it was cut off.
#: 100, not 400: every bucket costs six sequential sub-queries, so 400 was
#: still 2400 round trips in one request -- and a 400-point line chart is
#: unreadable anyway, so the old ceiling bought nothing a caller wanted.
MAX_BUCKETS = 100

#: The same cap for `groupBy=agent`/`department`, which had NONE: those
#: enumerate rows rather than derive them from a date range, so a tenant with
#: thousands of agents could turn one authenticated request into thousands of
#: `compute_kpis` calls -- six sub-queries each -- with nothing bounding it.
#: Checked from a COUNT before any per-group query runs, the same
#: reject-don't-truncate shape as MAX_BUCKETS: a silently shortened breakdown
#: is a table that looks complete and is quietly missing rows.
MAX_GROUPS = 100

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_bound(raw: str | None, *, field: str, end_of_day: bool) -> dt.datetime | None:
    """Parse a `dateFrom`/`dateTo` bound. Mirrors `audit.py::_parse_bound`
    (same field, same two reasons, read in full before writing this):

    1. A date-only value (an `<input type="date">`) must cover the WHOLE day
       it names, not silently mean midnight -- `dateTo=2026-07-21` has to
       include everything that happened on the 21st.
    2. A naive value must be anchored to UTC rather than resolved against
       whatever timezone the API process happens to be running in. Every
       timestamp `compute_kpis` compares against (`AgentRun.created_at`,
       `ApprovalRequest.created_at`/`decided_at`, `ChatMessage.created_at`) is
       `timestamptz`, stored and read in UTC -- and this is also what stops
       `dateFrom` (naive) from being compared against `dt.datetime.now(dt.UTC)`
       (aware) in the bucket loop below and raising `TypeError` outright.

    An explicit offset is honoured as given.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        if _DATE_ONLY.match(s):
            day = dt.date.fromisoformat(s)
            parsed = dt.datetime.combine(day, dt.time.max if end_of_day else dt.time.min)
        else:
            parsed = dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid '{field}' timestamp",
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _floor_to_unit(start: dt.datetime, group_by: str) -> dt.datetime:
    """Align a bucket series' first boundary to the calendar unit it is
    grouped by, so every bucket -- not just its label -- covers a genuine
    day/week/month.

    Without this, `range_start` keeps whatever time-of-day it started with
    (the default is `now - 30 days`, i.e. THIS INSTANT's time-of-day; an
    explicit `dateFrom` keeps whatever time-of-day the caller happened to
    send), and every bucket boundary inherits that offset while its label
    (`cursor.date().isoformat()`) implies a clean midnight-to-midnight day.
    A run at 09:00 can then land in the bucket whose boundary is, say,
    14:32 the day before -- and get reported under the PREVIOUS day's label.
    Flooring `range_start` alone is enough: every later boundary is derived
    from it by whole-unit steps (`_dt.timedelta(days=1)`/`weeks=1`, or a
    roll to the 1st of the next month), so once the first one is aligned,
    all of them are.
    """
    floored = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if group_by == "week":
        floored -= dt.timedelta(days=floored.weekday())
    elif group_by == "month":
        floored = floored.replace(day=1)
    return floored


def _group_id_query(
    group_by: str,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
) -> Select[tuple[uuid.UUID]]:
    """The group values a `groupBy=agent`/`department` request will produce,
    intersected with whatever `agentId`/`departmentId` narrowing the caller
    already asked for. Tenant-scoped explicitly, like every other query in this
    module; soft-deleted rows and the tenant Assistant's own agent/department
    are excluded -- the same set `agents.repo.visible_agents` /
    `departments.repo.visible_departments` list.

    The Assistant exclusion is not cosmetic. It is filtered out of both those
    funnels, so `GET /agents/{id}` and the per-agent KPI route already 404 for
    it. Enumerating it HERE produced a breakdown row for an agent whose own
    detail routes deny it exists -- a row nobody can click through. This query
    does not go through the funnels (it selects ids, not rows, and answers for
    a caller who has already passed the route's own scope check), so the flag
    terms are mirrored rather than inherited; the two funnels remain the place
    the rule is stated.

    `department` enumerates the DEPARTMENT table, not `Agent.department_id`.
    Deriving the list from agents dropped any department with no live agent in
    it -- silently, producing no row at all rather than a zero one -- even
    though such a department can perfectly well hold decided `ApprovalRequest`
    rows, which carry `department_id` denormalised on themselves and need no
    agent to exist (spec §1.3). A department that exists and has numbers to
    report belongs in the response; one that exists with nothing to report
    belongs there as zeroes, which is a different statement from absence.
    """
    if group_by == "agent":
        stmt = select(m.Agent.id).where(
            m.Agent.tenant_id == tenant_id,
            m.Agent.deleted_at.is_(None),
            m.Agent.is_tenant_assistant.is_(False),
            # Only agents of a `standard` department, matching the department
            # breakdown below: a personal department's agents get no row here.
            m.Agent.department_id.in_(
                select(m.Department.id).where(m.Department.kind == DEPARTMENT_KIND_STANDARD)
            ),
        )
        if agent_id is not None:
            stmt = stmt.where(m.Agent.id == agent_id)
        if department_id is not None:
            stmt = stmt.where(m.Agent.department_id == department_id)
        return stmt
    dept_stmt = select(m.Department.id).where(
        m.Department.tenant_id == tenant_id,
        m.Department.deleted_at.is_(None),
        m.Department.kind == DEPARTMENT_KIND_STANDARD,
    )
    if department_id is not None:
        dept_stmt = dept_stmt.where(m.Department.id == department_id)
    if agent_id is not None:
        # An explicit agentId narrows the breakdown to that agent's own
        # department -- the one department whose rows the caller asked about.
        dept_stmt = dept_stmt.where(
            m.Department.id.in_(
                select(m.Agent.department_id).where(
                    m.Agent.tenant_id == tenant_id, m.Agent.id == agent_id
                )
            )
        )
    return dept_stmt


def _to_dto(result: KPIResult) -> KPIDTO:
    return KPIDTO(
        run_count=result.run_count,
        total_duration_ms=result.total_duration_ms,
        execution_duration_ms=result.execution_duration_ms,
        approval_wait_ms=result.approval_wait_ms,
        response_time_ms=result.response_time_ms,
        avg_tool_call_duration_ms=result.avg_tool_call_duration_ms,
    )


def _to_group_row(result: KPIResult, group_key: str) -> dict[str, object]:
    return KPIGroupRowDTO(
        run_count=result.run_count,
        total_duration_ms=result.total_duration_ms,
        execution_duration_ms=result.execution_duration_ms,
        approval_wait_ms=result.approval_wait_ms,
        response_time_ms=result.response_time_ms,
        avg_tool_call_duration_ms=result.avg_tool_call_duration_ms,
        group_key=group_key,
    ).model_dump(by_alias=True)


@router.get(
    "/agents/{agent_id}/kpis",
    response_model=KPIDTO,
)
async def agent_kpis(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
    date_from: Annotated[str | None, Query(alias="dateFrom")] = None,
    date_to: Annotated[str | None, Query(alias="dateTo")] = None,
) -> KPIDTO:
    parsed_from = _parse_bound(date_from, field="dateFrom", end_of_day=False)
    parsed_to = _parse_bound(date_to, field="dateTo", end_of_day=True)
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
    agent = await visible_agent(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide,
        agent_id=agent_id,
        member_id=actor.member.id,
        manage_all=tenant_wide_read(authority, perm(DEPARTMENT, MANAGE)),
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    result = await compute_kpis(
        db,
        tenant_id=actor.principal.tenant_id,
        agent_id=agent_id,
        date_from=parsed_from,
        date_to=parsed_to,
    )
    return _to_dto(result)


@router.get(
    "/departments/{dept_id}/kpis",
    response_model=KPIDTO,
)
async def department_kpis(
    dept_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(DEPARTMENT, VIEW)))],
    date_from: Annotated[str | None, Query(alias="dateFrom")] = None,
    date_to: Annotated[str | None, Query(alias="dateTo")] = None,
) -> KPIDTO:
    parsed_from = _parse_bound(date_from, field="dateFrom", end_of_day=False)
    parsed_to = _parse_bound(date_to, field="dateTo", end_of_day=True)
    authority = await authority_for_principal(request, db, actor.principal)
    tenant_wide = tenant_wide_read(authority, perm(DEPARTMENT, VIEW))
    dept = await visible_department(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide,
        department_id=dept_id,
        member_id=actor.member.id,
        manage_all=tenant_wide_read(authority, perm(DEPARTMENT, MANAGE)),
    )
    if dept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
    result = await compute_kpis(
        db,
        tenant_id=actor.principal.tenant_id,
        department_id=dept_id,
        date_from=parsed_from,
        date_to=parsed_to,
    )
    return _to_dto(result)


@router.get("/kpis", dependencies=[Depends(_require_stats)])
async def tenant_kpis(
    db: DbSession,
    principal: CurrentPrincipal,
    agent_id: Annotated[uuid.UUID | None, Query(alias="agentId")] = None,
    department_id: Annotated[uuid.UUID | None, Query(alias="departmentId")] = None,
    date_from: Annotated[str | None, Query(alias="dateFrom")] = None,
    date_to: Annotated[str | None, Query(alias="dateTo")] = None,
    group_by: Annotated[str | None, Query(alias="groupBy")] = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, object]:
    """Tenant-wide KPIs, optionally filtered by agent/department/date range/
    run status, and optionally grouped.

    Ungrouped returns a bare `KPIDTO`-shaped dict (`response_model` is left
    unset -- see `schemas/kpi.py` -- because the grouped shape below is a
    different DTO entirely and FastAPI cannot express the union cleanly).

    `group_by=agent`/`department` enumerates the group values THIS TENANT
    actually has (see `_group_id_query`, including why departments come from
    the `department` table rather than from `Agent.department_id`) and calls
    `compute_kpis` once per value, capped at `MAX_GROUPS`.
    Soft-deleted agents are excluded from the enumeration -- same set
    `agents.repo.visible_agents` lists by default -- which is why a
    department's grouped-by-agent rows will not sum to that department's own
    total once an agent under it has been deleted: the department-scoped
    total (`compute_kpis`'s own deliberate choice, kpis/aggregate.py) keeps
    counting a deleted agent's historical runs, but there is no live agent
    row left to enumerate a breakdown row for it. Not a bug in either place.

    `group_by=day`/`week`/`month` buckets `[date_from, date_to]` in Python,
    defaulting to the last 30 days when neither bound is given -- an
    unbounded tenant-wide time series would be needlessly expensive -- and
    calls `compute_kpis` once per bucket. See `compute_kpis`'s own docstring
    for why this grouping lives here rather than as a second SQL code path
    inside every one of its six sub-queries. `range_start` is floored to the
    grouping unit (`_floor_to_unit`) before bucketing starts, and the number
    of buckets a request can produce is capped (`MAX_BUCKETS`) rather than
    silently truncated.
    """
    if group_by is not None and group_by not in _GROUP_BY_VALUES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"groupBy must be one of {sorted(_GROUP_BY_VALUES)}, got {group_by!r}",
        )
    tenant_id = principal.tenant_id
    parsed_from = _parse_bound(date_from, field="dateFrom", end_of_day=False)
    parsed_to = _parse_bound(date_to, field="dateTo", end_of_day=True)

    if group_by is None:
        result = await compute_kpis(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            department_id=department_id,
            date_from=parsed_from,
            date_to=parsed_to,
            status=status_,
        )
        return _to_dto(result).model_dump(by_alias=True)

    if group_by in ("agent", "department"):
        ids_stmt = _group_id_query(
            group_by, tenant_id=tenant_id, agent_id=agent_id, department_id=department_id
        )
        group_count = int(
            (await db.execute(select(func.count()).select_from(ids_stmt.subquery()))).scalar_one()
        )
        if group_count > MAX_GROUPS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"groupBy={group_by} would produce {group_count} groups, more than the "
                f"{MAX_GROUPS} this endpoint returns; narrow the request with "
                "agentId/departmentId",
            )
        group_ids = [row for row in (await db.execute(ids_stmt)).scalars().all() if row is not None]

        rows = []
        for gid in group_ids:
            result = await compute_kpis(
                db,
                tenant_id=tenant_id,
                agent_id=gid if group_by == "agent" else agent_id,
                department_id=gid if group_by == "department" else department_id,
                date_from=parsed_from,
                date_to=parsed_to,
                status=status_,
            )
            rows.append(_to_group_row(result, str(gid)))
        return {"rows": rows}

    # group_by in ("day", "week", "month"): bucket [date_from, date_to].
    step = {"day": dt.timedelta(days=1), "week": dt.timedelta(weeks=1), "month": None}[group_by]
    range_end = parsed_to or dt.datetime.now(dt.UTC)
    range_start = _floor_to_unit(parsed_from or (range_end - dt.timedelta(days=30)), group_by)

    buckets: list[tuple[dt.datetime, dt.datetime, str]] = []
    cursor = range_start
    while cursor < range_end:
        if len(buckets) >= MAX_BUCKETS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"dateFrom/dateTo with groupBy={group_by} would produce more than "
                f"{MAX_BUCKETS} buckets; narrow the date range or choose a coarser groupBy",
            )
        if step is None:  # month: roll to the 1st of the next calendar month
            if cursor.month == 12:
                bucket_end = cursor.replace(year=cursor.year + 1, month=1, day=1)
            else:
                bucket_end = cursor.replace(month=cursor.month + 1, day=1)
            bucket_end = min(bucket_end, range_end)
        else:
            bucket_end = min(cursor + step, range_end)
        buckets.append((cursor, bucket_end, cursor.date().isoformat()))
        cursor = bucket_end

    bucket_rows = []
    for bucket_from, bucket_to, label in buckets:
        result = await compute_kpis(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            department_id=department_id,
            date_from=bucket_from,
            date_to=bucket_to,
            status=status_,
        )
        bucket_rows.append(_to_group_row(result, label))
    return {"rows": bucket_rows}


def _feedback_key(key: str) -> ColumnElement[str]:
    """`resource ->> '<key>'` of an audit event; `key` is one of this
    module's constants."""
    return m.AuditEvent.resource.op("->>", return_type=Text)(literal_column(f"'{key}'"))


@router.get(
    "/stats/feedback",
    response_model=list[FeedbackCountDTO],
    dependencies=[Depends(_require_stats)],
)
async def feedback_counts(
    db: DbSession,
    principal: CurrentPrincipal,
    agent_id: Annotated[uuid.UUID | None, Query(alias="agentId")] = None,
    department_id: Annotated[uuid.UUID | None, Query(alias="departmentId")] = None,
    date_from: Annotated[str | None, Query(alias="dateFrom")] = None,
    date_to: Annotated[str | None, Query(alias="dateTo")] = None,
) -> list[FeedbackCountDTO]:
    """Thumbs up and down per agent and model, read from the `chat.feedback`
    audit events -- the only place a rating is kept. Rating an answer again
    appends another event, so only each person's latest rating of an answer
    counts. Counts only, never a note.

    Same gate and filters as `GET /kpis`, and the same rows as its
    `groupBy=agent` breakdown: `statistics:view` is tenant-wide only (no seat
    narrows it), and only agents of a `standard` department get a row -- a
    personal agent's ratings are the owner's, like its runs there. An event
    written before it named its agent is attributed through its chat; one
    whose agent cannot be resolved at all is left out (fail closed: it may
    have been a personal agent's). An `agentId`/`departmentId` that names no
    standard agent/department of the tenant is a 404. The period bounds the
    rating's own time."""
    tenant_id = principal.tenant_id
    if agent_id is not None:
        found = await db.scalar(
            select(m.Agent.id)
            .join(m.Department, m.Department.id == m.Agent.department_id)
            .where(
                m.Agent.tenant_id == tenant_id,
                m.Agent.id == agent_id,
                m.Department.kind == DEPARTMENT_KIND_STANDARD,
            )
        )
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if department_id is not None:
        found = await db.scalar(
            select(m.Department.id).where(
                m.Department.tenant_id == tenant_id,
                m.Department.id == department_id,
                m.Department.kind == DEPARTMENT_KIND_STANDARD,
            )
        )
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
    parsed_from = _parse_bound(date_from, field="dateFrom", end_of_day=False)
    parsed_to = _parse_bound(date_to, field="dateTo", end_of_day=True)
    latest_q = (
        select(
            _feedback_key("agent_id").label("agent_txt"),
            _feedback_key("session_id").label("session_txt"),
            _feedback_key("model").label("model"),
            _feedback_key("rating").label("rating"),
        )
        .where(
            m.AuditEvent.tenant_id == tenant_id,
            m.AuditEvent.action == "chat.feedback",
        )
        .distinct(_feedback_key("message_id"), m.AuditEvent.actor_id)
        .order_by(_feedback_key("message_id"), m.AuditEvent.actor_id, m.AuditEvent.seq.desc())
    )
    if parsed_from is not None:
        latest_q = latest_q.where(m.AuditEvent.ts >= parsed_from)
    if parsed_to is not None:
        latest_q = latest_q.where(m.AuditEvent.ts <= parsed_to)
    latest = latest_q.subquery()
    agent_key = func.coalesce(cast(latest.c.agent_txt, Uuid), m.ChatSession.agent_id)
    stmt = (
        select(
            m.Agent.id,
            m.Agent.name,
            latest.c.model,
            func.count().filter(latest.c.rating == "up"),
            func.count().filter(latest.c.rating == "down"),
        )
        .select_from(latest)
        .outerjoin(
            m.ChatSession,
            (m.ChatSession.id == cast(latest.c.session_txt, Uuid))
            & (m.ChatSession.tenant_id == tenant_id),
        )
        .join(m.Agent, (m.Agent.id == agent_key) & (m.Agent.tenant_id == tenant_id))
        .join(
            m.Department,
            (m.Department.id == m.Agent.department_id)
            & (m.Department.kind == DEPARTMENT_KIND_STANDARD),
        )
        .group_by(m.Agent.id, m.Agent.name, latest.c.model)
        .order_by(m.Agent.name, latest.c.model)
    )
    if agent_id is not None:
        stmt = stmt.where(m.Agent.id == agent_id)
    if department_id is not None:
        stmt = stmt.where(m.Agent.department_id == department_id)
    rows = (await db.execute(stmt)).all()
    return [
        FeedbackCountDTO(
            agent_id=str(aid),
            agent_name=name,
            model=model,
            up=up,
            down=down,
        )
        for aid, name, model, up, down in rows
    ]
