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
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, func, select
from sqlalchemy.exc import SQLAlchemyError

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1._export_utils import (
    csv_safe as _csv_safe,  # shared with usage export; kept aliased to minimise this module's diff
)
from oc8.api.v1._export_utils import (
    parse_date_bound as _parse_bound,
)
from oc8.audit.integrity import (
    BATCH as BATCH,  # re-exported: tests monkeypatch this name
)
from oc8.audit.integrity import get_checkpoint, verify_full, verify_incremental
from oc8.authz.permissions import AUDIT, AUDIT_VERIFY, VIEW, perm
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
    return stmt


@router.get("/audit", response_model=AuditPageDTO, dependencies=[Depends(_require_read)])
async def list_audit_events(
    db: DbSession,
    category: str | None = None,
    action: str | None = None,
    actor_type: str | None = None,
    decision: str | None = None,
    responsible_type: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query(alias="to")] = None,
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
    )
    if before_seq is not None:
        stmt = stmt.where(m.AuditEvent.seq < before_seq)
    rows = (
        (await db.execute(stmt.order_by(m.AuditEvent.seq.desc()).limit(limit + 1))).scalars().all()
    )
    page = rows[:limit]
    next_before = page[-1].seq if len(rows) > limit and page else None
    return AuditPageDTO(events=[_event_to_dto(e) for e in page], next_before_seq=next_before)


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
    format: Literal["csv", "jsonl"] = "csv",
    category: str | None = None,
    action: str | None = None,
    actor_type: str | None = None,
    decision: str | None = None,
    responsible_type: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query(alias="to")] = None,
) -> StreamingResponse:
    from_ts = _parse_bound(from_, field="from", end_of_day=False)
    to_ts = _parse_bound(to, field="to", end_of_day=True)
    base = _filtered(
        select(m.AuditEvent),
        category=category,
        action=action,
        actor_type=actor_type,
        decision=decision,
        responsible_type=responsible_type,
        from_ts=from_ts,
        to_ts=to_ts,
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
            for ev in rows:
                dto = _event_to_dto(ev)
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
