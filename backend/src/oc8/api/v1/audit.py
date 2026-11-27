"""Auditor read surface over the per-tenant audit hash chain (tech-spec §12.5).

Read-only by construction: audit_event is append-only at the database level, and
nothing in this module writes to it. Every endpoint is org_admin-gated -- the log
carries approval reasons, policy decisions and supervision verdicts.

Follow-up: once an `auditor` role exists, widen the gate to
the `auditor` role now holds audit:view and audit:verify, and nothing else
reaches the trail. No other change is needed.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import ColumnElement, Select, Text, func, literal_column, or_, select
from sqlalchemy.exc import SQLAlchemyError

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1._export_utils import (
    csv_safe as _csv_safe,  # shared with usage export; kept aliased to minimise this module's diff
)
from oc8.api.v1._export_utils import (
    parse_date_bound as _parse_bound,
)
from oc8.approvals.repo import ADMIN_KEPT_ACTIONS, admin_kept_approval_ids
from oc8.audit.integrity import (
    BATCH as BATCH,  # re-exported: tests monkeypatch this name
)
from oc8.audit.integrity import get_checkpoint, verify_full, verify_incremental
from oc8.auth.principal import Principal
from oc8.authz.permissions import AUDIT, AUDIT_VERIFY, VIEW, perm
from oc8.authz.scope import scope_for_principal
from oc8.departments.repo import personal_content_hidden
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from oc8.schemas.dto import AuditEventDTO, AuditIntegrityDTO, AuditPageDTO
from oc8.secrets.keyprovider import SecretError

router = APIRouter()

#: Reading the trail is the auditor's own grant, not a side effect of being
#: an administrator -- see oc8.authz.permissions. Verifying it writes
#: checkpoints, so it is a separate right that the auditor also holds: the
#: role exists to check the log without being able to change what the log is
#: about.
_require_read = require_permission(perm(AUDIT, VIEW))
_require_verify = require_permission(AUDIT_VERIFY)

MAX_LIMIT = 200


#: What a personal agent's event may not show anybody but its owner: the
#: content the event was about (a mission before and after, a memory snippet,
#: a tool call's arguments, a search query, a task). The ids, the action, who
#: and when stay -- the entry is kept, only what the owner wrote or the agent
#: did is not repeated to an administrator.
_PERSONAL_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "before",
        "after",
        "mission",
        "instruction",
        "instructions",
        "prompt",
        "prompt_starters",
        "persona",
        "task",
        "task_text",
        "content",
        "content_snippet",
        "arguments",
        "query",
        "question",
        "answer",
        "text",
        "body",
    }
)

#: Events whose `before`/`after` hold metadata an admin already reads on the
#: agent itself -- its name, its autonomy level -- not content. Every other
#: personal-agent event keeps them stripped (a mission before and after).
_METADATA_CHANGE_ACTIONS: frozenset[str] = frozenset({"agent.renamed", "agent.autonomy.updated"})


def _content_keys(action: str) -> frozenset[str]:
    if action in _METADATA_CHANGE_ACTIONS:
        return _PERSONAL_CONTENT_KEYS - {"before", "after"}
    return _PERSONAL_CONTENT_KEYS


#: Is this personal department's content hidden from the reader?
_HidesDepartment = Callable[[uuid.UUID], bool]


async def _personal_reader(db: DbSession, principal: Principal) -> _HidesDepartment:
    """The reader's side of the personal-content rule, resolved once per
    request: the owner's seat sees its own department's content, everybody
    else (an auditor, an admin, a token with no member row) sees none."""
    try:
        _member, scope = await scope_for_principal(db, principal)
    except PermissionError:
        return lambda _department_id: True
    return lambda department_id: personal_content_hidden(
        kind=DEPARTMENT_KIND_PERSONAL, department_id=department_id, scope=scope
    )


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _subjects(ev: m.AuditEvent) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """The agents and departments one event is about: `agent_id` and
    `department_id` in its resource, and the acting agent of an agent event
    (a tool call carries no `agent_id` of its own)."""
    resource = ev.resource if isinstance(ev.resource, dict) else {}
    agents = {a for a in (_as_uuid(resource.get("agent_id")),) if a is not None}
    if ev.actor_type == "agent" and ev.actor_id is not None:
        agents.add(ev.actor_id)
    departments = {d for d in (_as_uuid(resource.get("department_id")),) if d is not None}
    return agents, departments


async def _events_to_dtos(
    db: DbSession, events: Sequence[m.AuditEvent], hides: _HidesDepartment
) -> list[AuditEventDTO]:
    """`_event_to_dto` for one page, with a personal agent's content stripped
    for every reader but its owner. One query per page resolves which of the
    page's agents and departments are personal, archived and deleted ones
    included, so rows written before this rule existed are covered too."""
    subjects = [_subjects(ev) for ev in events]
    agent_ids = set().union(*(a for a, _d in subjects)) if subjects else set()
    department_ids = set().union(*(d for _a, d in subjects)) if subjects else set()
    personal_agents: dict[uuid.UUID, uuid.UUID] = {}
    personal_departments: set[uuid.UUID] = set()
    if agent_ids or department_ids:
        rows = await db.execute(
            select(m.Department.id, m.Agent.id)
            .select_from(m.Department)
            .outerjoin(
                m.Agent,
                (m.Agent.department_id == m.Department.id) & m.Agent.id.in_(agent_ids),
            )
            .where(
                m.Department.kind == DEPARTMENT_KIND_PERSONAL,
                or_(m.Department.id.in_(department_ids), m.Agent.id.in_(agent_ids)),
            )
        )
        for department_id, agent_id in rows.all():
            personal_departments.add(department_id)
            if agent_id is not None:
                personal_agents[agent_id] = department_id
    kept_reasons = await _admin_kept_reasons(db, events)
    out: list[AuditEventDTO] = []
    for ev, (agents, departments) in zip(events, subjects, strict=True):
        owners = {personal_agents[a] for a in agents if a in personal_agents}
        owners |= departments & personal_departments
        dto = _event_to_dto(ev)
        if any(hides(d) for d in owners):
            if isinstance(dto.resource, dict):
                stripped = _content_keys(ev.action)
                dto.resource = {k: v for k, v in dto.resource.items() if k not in stripped}
            # A reason is free text, and a denial or a decision can quote what
            # the agent tried: a backstop beside the content keys, kept off
            # every reader but the owner. The decision itself stays -- and so
            # does the reason of an event about what the admins keep (T4-4).
            if ev.id not in kept_reasons:
                dto.reason = None
        out.append(dto)
    return out


async def _admin_kept_reasons(db: DbSession, events: Sequence[m.AuditEvent]) -> set[uuid.UUID]:
    """The events of one page whose `reason` stays visible to an admin although
    their agent is personal (ruling T4-4): what the admins see and decide
    themselves (ruling R2, U3) -- a budget event, and the decision of an
    `ADMIN_KEPT_ACTIONS` card (budget incident, blast radius), whose reason
    is often the admin's own. The card's kind is read from the decision's
    resource, or, for a line written before it carried one, from the card in
    one query per page. A card that resolves to nothing keeps its reason
    stripped (fail closed)."""
    kept: set[uuid.UUID] = set()
    lookup: dict[uuid.UUID, uuid.UUID] = {}
    for ev in events:
        if ev.category == "budget":
            kept.add(ev.id)
            continue
        if not ev.action.startswith("approval."):
            continue
        resource = ev.resource if isinstance(ev.resource, dict) else {}
        action_type = resource.get("action_type")
        if isinstance(action_type, str):
            if action_type in ADMIN_KEPT_ACTIONS:
                kept.add(ev.id)
            continue
        approval_id = _as_uuid(resource.get("approval_id"))
        if approval_id is not None:
            lookup[ev.id] = approval_id
    if lookup:
        found = await admin_kept_approval_ids(db, set(lookup.values()))
        kept |= {ev_id for ev_id, approval_id in lookup.items() if approval_id in found}
    return kept


def _event_to_dto(ev: m.AuditEvent) -> AuditEventDTO:
    return AuditEventDTO(
        id=str(ev.id),
        seq=ev.seq,
        ts=ev.ts.isoformat(),
        actor_type=ev.actor_type,
        actor_id=str(ev.actor_id) if ev.actor_id else None,
        category=ev.category,
        action=ev.action,
        resource=ev.resource,
        decision=ev.decision,
        reason=ev.reason,
        responsible_type=ev.responsible_type,
        responsible_id=ev.responsible_id,
        hash=ev.hash.hex(),
        prev_hash=ev.prev_hash.hex(),
    )


def _filtered(
    stmt: Select[tuple[m.AuditEvent]],
    *,
    category: str | None,
    action: str | None,
    actor_type: str | None,
    decision: str | None,
    responsible_type: str | None,
    from_ts: dt.datetime | None,
    to_ts: dt.datetime | None,
    run_id: uuid.UUID | None = None,
    chat_session_id: uuid.UUID | None = None,
) -> Select[tuple[m.AuditEvent]]:
    if category:
        stmt = stmt.where(m.AuditEvent.category == category)
    if action:
        stmt = stmt.where(m.AuditEvent.action == action)
    if actor_type:
        stmt = stmt.where(m.AuditEvent.actor_type == actor_type)
    if decision:
        stmt = stmt.where(m.AuditEvent.decision == decision)
    if responsible_type:
        stmt = stmt.where(m.AuditEvent.responsible_type == responsible_type)
    if from_ts:
        stmt = stmt.where(m.AuditEvent.ts >= from_ts)
    if to_ts:
        stmt = stmt.where(m.AuditEvent.ts <= to_ts)
    # Every event that names the run or chat in `resource`, tool calls
    # included (spec §5.7).
    if run_id is not None:
        stmt = stmt.where(_resource_key("run_id") == str(run_id))
    if chat_session_id is not None:
        stmt = stmt.where(_resource_key("chat_session_id") == str(chat_session_id))
    return stmt


def _resource_key(key: str) -> ColumnElement[str]:
    """`resource ->> '<key>'` with the key as a literal, never a bound
    parameter: only then is it the expression migration 0138 indexes, under a
    generic prepared plan as well. `key` is one of this module's constants."""
    return m.AuditEvent.resource.op("->>", return_type=Text)(literal_column(f"'{key}'"))


@router.get("/audit", response_model=AuditPageDTO, dependencies=[Depends(_require_read)])
async def list_audit_events(
    db: DbSession,
    principal: CurrentPrincipal,
    category: str | None = None,
    action: str | None = None,
    actor_type: str | None = None,
    decision: str | None = None,
    responsible_type: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query(alias="to")] = None,
    run_id: uuid.UUID | None = None,
    chat_session_id: uuid.UUID | None = None,
    before_seq: int | None = None,
    limit: int = 50,
) -> AuditPageDTO:
    limit = max(1, min(limit, MAX_LIMIT))
    from_ts = _parse_bound(from_, field="from", end_of_day=False)
    to_ts = _parse_bound(to, field="to", end_of_day=True)
    stmt = _filtered(
        select(m.AuditEvent),
        category=category,
        action=action,
        actor_type=actor_type,
        decision=decision,
        responsible_type=responsible_type,
        from_ts=from_ts,
        to_ts=to_ts,
        run_id=run_id,
        chat_session_id=chat_session_id,
    )
    if before_seq is not None:
        stmt = stmt.where(m.AuditEvent.seq < before_seq)
    rows = (
        (await db.execute(stmt.order_by(m.AuditEvent.seq.desc()).limit(limit + 1))).scalars().all()
    )
    page = rows[:limit]
    next_before = page[-1].seq if len(rows) > limit and page else None
    hides = await _personal_reader(db, principal)
    return AuditPageDTO(events=await _events_to_dtos(db, page, hides), next_before_seq=next_before)


# Column order is stable; column NAMES are the AuditEventDTO camelCase aliases
# (matching GET /audit and the JSONL branch below) so a CSV export and a JSONL
# export of the same log key identically for an auditor's ETL.
_EXPORT_COLUMNS = [
    "seq",
    "ts",
    "actorType",
    "actorId",
    "category",
    "action",
    "resource",
    "decision",
    "reason",
    "responsibleType",
    "responsibleId",
    "hash",
    "prevHash",
]


def _row(dto: AuditEventDTO) -> list[str]:
    # Single by_alias dump, indexed by the same alias keys used for the header,
    # so header and values cannot drift apart.
    d = dto.model_dump(by_alias=True)
    out: list[str] = []
    for col in _EXPORT_COLUMNS:
        val = d[col]
        cell = (
            json.dumps(val, sort_keys=True)
            if col == "resource"
            else ("" if val is None else str(val))
        )
        out.append(_csv_safe(cell))
    return out


@router.get("/audit/export", dependencies=[Depends(_require_read)])
async def export_audit_events(
    db: DbSession,
    principal: CurrentPrincipal,
    format: Literal["csv", "jsonl"] = "csv",
    category: str | None = None,
    action: str | None = None,
    actor_type: str | None = None,
    decision: str | None = None,
    responsible_type: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query(alias="to")] = None,
    run_id: uuid.UUID | None = None,
    chat_session_id: uuid.UUID | None = None,
) -> StreamingResponse:
    from_ts = _parse_bound(from_, field="from", end_of_day=False)
    to_ts = _parse_bound(to, field="to", end_of_day=True)
    hides = await _personal_reader(db, principal)
    base = _filtered(
        select(m.AuditEvent),
        category=category,
        action=action,
        actor_type=actor_type,
        decision=decision,
        responsible_type=responsible_type,
        from_ts=from_ts,
        to_ts=to_ts,
        run_id=run_id,
        chat_session_id=chat_session_id,
    )

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
            writer.writerow(_EXPORT_COLUMNS)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
        cursor = 0
        while True:
            rows = (
                (
                    await db.execute(
                        base.where(m.AuditEvent.seq > cursor)
                        .order_by(m.AuditEvent.seq.asc())
                        .limit(BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return
            for ev, dto in zip(rows, await _events_to_dtos(db, rows, hides), strict=True):
                if format == "csv":
                    writer.writerow(_row(dto))
                else:
                    buf.write(json.dumps(dto.model_dump(by_alias=True), default=str) + "\n")
                cursor = ev.seq
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    media = "text/csv" if format == "csv" else "application/x-ndjson"
    return StreamingResponse(
        _stream(),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="audit-export.{format}"'},
    )


async def _integrity_dto(db: DbSession, cp: m.AuditChainCheckpoint | None) -> AuditIntegrityDTO:
    count = (await db.execute(select(func.count()).select_from(m.AuditEvent))).scalar_one()
    if cp is None:
        return AuditIntegrityDTO(status="never", verified_through_seq=0, event_count=count)
    missing_count: int | None = None
    if cp.break_kind == "truncation":
        # verified_count is how many rows existed at/below max_seen_seq at the
        # last clean pass; present is how many of those are still there now.
        # Their difference is exact regardless of WHICH rows are missing --
        # unlike broken_at_seq, it supports no claim about a range.
        present = (
            await db.execute(
                select(func.count())
                .select_from(m.AuditEvent)
                .where(m.AuditEvent.seq <= cp.max_seen_seq)
            )
        ).scalar_one()
        missing_count = cp.verified_count - present
    return AuditIntegrityDTO(
        status=cp.status,
        verified_through_seq=cp.last_seq,
        head_hash=cp.last_hash.hex(),
        verified_at=cp.verified_at.isoformat(),
        broken_at_seq=cp.broken_at_seq,
        break_kind=cp.break_kind,
        first_break_at=cp.first_break_at.isoformat() if cp.first_break_at else None,
        missing_count=missing_count,
        event_count=count,
    )


@router.get(
    "/audit/integrity",
    response_model=AuditIntegrityDTO,
    dependencies=[Depends(_require_read)],
)
async def audit_integrity(db: DbSession, principal: CurrentPrincipal) -> AuditIntegrityDTO:
    return await _integrity_dto(db, await get_checkpoint(db, principal.tenant_id))


@router.post(
    "/audit/verify",
    response_model=AuditIntegrityDTO,
    dependencies=[Depends(_require_verify)],
)
async def audit_verify(
    db: DbSession, principal: CurrentPrincipal, full: bool = False
) -> AuditIntegrityDTO:
    try:
        cp = (
            await verify_full(db, principal.tenant_id)
            if full
            else await verify_incremental(db, principal.tenant_id)
        )
    except (SQLAlchemyError, SecretError) as exc:
        # Never report "ok" for a run that did not finish -- leave the stored
        # checkpoint untouched and make the failure visible.
        #
        # SecretError is belt and braces: verify_* turns a missing MAC key into
        # the "unverifiable" status rather than raising, but a SecretError that
        # does reach here is still a run that could not conclude, and 503 says
        # that. A 500 would read as a server bug.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="chain verification failed",
        ) from exc
    return await _integrity_dto(db, cp)
