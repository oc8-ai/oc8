"""Handoff types + lifecycle endpoints (§14a.2)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import HANDOFF, MANAGE, VIEW, perm
from oc8.collab.flow_engine import on_handoff_completed
from oc8.collab.handoff import (
    HandoffError,
    HandoffStateError,
    PayloadInvalid,
    accept_handoff,
    complete_handoff,
    create_handoff,
    create_handoff_type,
    reject_handoff,
)
from oc8.collab.intake import route_to_team_lead
from oc8.schemas.base import CamelModel

router = APIRouter()


class HandoffTypeRequest(CamelModel):
    name: str
    payload_schema: dict[str, Any]
    classification: str = "internal"


class HandoffRequest(CamelModel):
    handoff_type_id: uuid.UUID
    source_department_id: uuid.UUID
    target_department_id: uuid.UUID
    payload: dict[str, Any]
    source_task_id: uuid.UUID | None = None
    gate: str = "auto"


class AcceptRequest(CamelModel):
    target_task_id: uuid.UUID | None = None


class HandoffTypeDTO(CamelModel):
    id: str
    name: str
    classification: str


class HandoffDTO(CamelModel):
    id: str
    handoff_type_id: str
    type: str = ""
    source_department_id: str
    target_department_id: str
    source_dept: str = ""
    target_dept: str = ""
    status: str
    gate: str
    payload: dict[str, Any] = {}
    created_by: str = ""
    created_at: str = ""
    target_task_id: str | None = None


async def _to_dto(db: DbSession, h: m.Handoff) -> HandoffDTO:
    ht = await db.get(m.HandoffType, h.handoff_type_id)
    src = await db.get(m.Department, h.source_department_id)
    tgt = await db.get(m.Department, h.target_department_id)
    return HandoffDTO(
        id=str(h.id),
        handoff_type_id=str(h.handoff_type_id),
        type=ht.name if ht is not None else "",
        source_department_id=str(h.source_department_id),
        target_department_id=str(h.target_department_id),
        source_dept=src.name if src is not None else "",
        target_dept=tgt.name if tgt is not None else "",
        status=h.status,
        gate=h.gate,
        payload=h.payload or {},
        created_by=str(h.created_by),
        created_at=h.created_at.isoformat() if h.created_at else "",
        target_task_id=str(h.target_task_id) if h.target_task_id else None,
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@router.post(
    "/handoff-types",
    response_model=HandoffTypeDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(HANDOFF, MANAGE)))],
)
async def define_type(
    body: HandoffTypeRequest, db: DbSession, principal: CurrentPrincipal
) -> HandoffTypeDTO:
    try:
        ht = await create_handoff_type(
            db,
            tenant_id=principal.tenant_id,
            name=body.name,
            payload_schema=body.payload_schema,
            classification=body.classification,
        )
    except HandoffError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return HandoffTypeDTO(id=str(ht.id), name=ht.name, classification=ht.classification)


@router.get(
    "/handoff-types",
    response_model=list[HandoffTypeDTO],
    dependencies=[Depends(require_permission(perm(HANDOFF, VIEW)))],
)
async def list_types(db: DbSession, principal: CurrentPrincipal) -> list[HandoffTypeDTO]:
    rows = (await db.execute(select(m.HandoffType))).scalars().all()
    return [
        HandoffTypeDTO(id=str(t.id), name=t.name, classification=t.classification) for t in rows
    ]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


async def _flows_using(db: DbSession, type_name: str) -> int:
    """Live flows whose current version has a stage handing off this type.
    Archived flows do not count: they start no runs."""
    rows = (
        await db.execute(
            select(m.FlowVersion.spec)
            .join(m.Flow, m.Flow.current_version_id == m.FlowVersion.id)
            .where(m.Flow.archived_at.is_(None))
        )
    ).scalars()
    return sum(
        1
        for spec in rows
        if any(
            isinstance(stage, dict)
            and isinstance(stage.get("handoff"), dict)
            and stage["handoff"].get("type") == type_name
            for stage in (spec or {}).get("stages", [])
        )
    )


@router.delete(
    "/handoff-types/{type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(HANDOFF, MANAGE)))],
)
async def delete_type(type_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> Response:
    """Delete a handoff type nothing uses. Handoffs keep their type for their
    history, so a type that was ever handed off stays; so does one a contract
    binding or a live flow still names. 409 says which."""
    ht = await db.get(m.HandoffType, type_id)
    if ht is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff type not found")
    handoffs = (
        await db.execute(
            select(func.count()).select_from(m.Handoff).where(m.Handoff.handoff_type_id == ht.id)
        )
    ).scalar_one()
    bindings = (
        await db.execute(
            select(func.count())
            .select_from(m.ContractBinding)
            .where(m.ContractBinding.handoff_type_id == ht.id)
        )
    ).scalar_one()
    flows = await _flows_using(db, ht.name)
    uses = [
        _plural(n, word)
        for n, word in (
            (handoffs, "handoff"),
            (bindings, "contract binding"),
            (flows, "flow"),
        )
        if n
    ]
    if uses:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'handoff type "{ht.name}" is still used by {", ".join(uses)}. '
            "Remove the contract bindings and archive the flows first; a type that "
            "handoffs were made with stays for their history.",
        )
    await db.delete(ht)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/handoffs",
    response_model=HandoffDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(HANDOFF, MANAGE)))],
)
async def create(body: HandoffRequest, db: DbSession, principal: CurrentPrincipal) -> HandoffDTO:
    ht = await db.get(m.HandoffType, body.handoff_type_id)
    if ht is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff type not found")
    try:
        handoff = await create_handoff(
            db,
            tenant_id=principal.tenant_id,
            handoff_type=ht,
            source_department_id=body.source_department_id,
            target_department_id=body.target_department_id,
            payload=body.payload,
            created_by=uuid.UUID(principal.subject)
            if _is_uuid(principal.subject)
            else uuid.uuid4(),
            source_task_id=body.source_task_id,
            gate=body.gate,
        )
    except PayloadInvalid as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await _to_dto(db, handoff)


@router.get(
    "/handoffs",
    response_model=list[HandoffDTO],
    dependencies=[Depends(require_permission(perm(HANDOFF, VIEW)))],
)
async def list_handoffs(db: DbSession, principal: CurrentPrincipal) -> list[HandoffDTO]:
    rows = (await db.execute(select(m.Handoff))).scalars().all()
    return [await _to_dto(db, h) for h in rows]


async def _load(db: DbSession, handoff_id: uuid.UUID) -> m.Handoff:
    h = await db.get(m.Handoff, handoff_id)
    if h is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff not found")
    return h


@router.post(
    "/handoffs/{handoff_id}/accept",
    response_model=HandoffDTO,
    dependencies=[Depends(require_permission(perm(HANDOFF, MANAGE)))],
)
async def accept(
    handoff_id: uuid.UUID, body: AcceptRequest, db: DbSession, principal: CurrentPrincipal
) -> HandoffDTO:
    h = await _load(db, handoff_id)
    try:
        await accept_handoff(db, h, target_task_id=body.target_task_id)
    except HandoffStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # §14a.3: an accepted intake "materializes as a new task assigned to the
    # team-lead agent, who decomposes it". Until this, accepting produced a row
    # and no work -- the caller had to supply a task id nobody was creating.
    # Skipped when the caller named a task itself: they know where it belongs.
    if body.target_task_id is None:
        await route_to_team_lead(db, h, tenant_id=principal.tenant_id)
    return await _to_dto(db, h)


@router.post(
    "/handoffs/{handoff_id}/reject",
    response_model=HandoffDTO,
    dependencies=[Depends(require_permission(perm(HANDOFF, MANAGE)))],
)
async def reject(handoff_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> HandoffDTO:
    h = await _load(db, handoff_id)
    try:
        await reject_handoff(db, h)
    except HandoffStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _to_dto(db, h)


@router.post(
    "/handoffs/{handoff_id}/complete",
    response_model=HandoffDTO,
    dependencies=[Depends(require_permission(perm(HANDOFF, MANAGE)))],
)
async def complete(handoff_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> HandoffDTO:
    h = await _load(db, handoff_id)
    try:
        await complete_handoff(db, h)
    except HandoffStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # If this handoff belongs to a flow run, completing it advances the flow.
    await on_handoff_completed(db, h)
    return await _to_dto(db, h)
