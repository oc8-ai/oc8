"""Contract bindings + department emit endpoint (§14a.3)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import CONTRACT, MANAGE, VIEW, perm
from oc8.collab.contracts import ContractViolation, department_emits
from oc8.collab.emit import emit_event
from oc8.schemas.base import CamelModel

router = APIRouter()


class BindingRequest(CamelModel):
    event_type: str
    handoff_type_id: uuid.UUID
    source_department_id: uuid.UUID
    target_department_id: uuid.UUID
    payload_map: dict[str, str] = {}
    gate: str = "auto"


class EmitRequest(CamelModel):
    event_type: str
    payload: dict[str, Any] = {}


class BindingDTO(CamelModel):
    id: str
    event_type: str
    handoff_type_id: str = ""
    source_department_id: str
    target_department_id: str


@router.post(
    "/contract-bindings",
    response_model=BindingDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(CONTRACT, MANAGE)))],
)
async def create_binding(
    body: BindingRequest, db: DbSession, principal: CurrentPrincipal
) -> BindingDTO:
    binding = m.ContractBinding(
        tenant_id=principal.tenant_id,
        event_type=body.event_type,
        handoff_type_id=body.handoff_type_id,
        source_department_id=body.source_department_id,
        target_department_id=body.target_department_id,
        payload_map=body.payload_map,
        gate=body.gate,
    )
    db.add(binding)
    await db.flush()
    return BindingDTO(
        id=str(binding.id),
        event_type=binding.event_type,
        handoff_type_id=str(binding.handoff_type_id),
        source_department_id=str(binding.source_department_id),
        target_department_id=str(binding.target_department_id),
    )


@router.get(
    "/contract-bindings",
    response_model=list[BindingDTO],
    dependencies=[Depends(require_permission(perm(CONTRACT, VIEW)))],
)
async def list_bindings(db: DbSession, principal: CurrentPrincipal) -> list[BindingDTO]:
    rows = (await db.execute(select(m.ContractBinding))).scalars().all()
    return [
        BindingDTO(
            id=str(b.id),
            event_type=b.event_type,
            handoff_type_id=str(b.handoff_type_id),
            source_department_id=str(b.source_department_id),
            target_department_id=str(b.target_department_id),
        )
        for b in rows
    ]


@router.delete(
    "/contract-bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(CONTRACT, MANAGE)))],
)
async def delete_binding(
    binding_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> Response:
    """Remove a binding: the event no longer creates handoffs for its target.
    Handoffs it already created do not point back at it and stay as they are."""
    binding = await db.get(m.ContractBinding, binding_id)
    if binding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contract binding not found")
    await db.delete(binding)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Department-frame contracts: emits + intakes (§14a.2) ------------------
# Emits/intakes are declared on Department.frame JSONB. Intake entries persist
# with `handoff_type` (the key the PEP reads in oc8.collab.contracts) plus the
# UI's route/gate/fields, so editing here stays the single source of truth.


class ContractFieldDTO(CamelModel):
    name: str
    type: str = "string"
    required: bool = False
    example: Any | None = None


class IntakeDTO(CamelModel):
    id: str
    type: str  # handoff_type name
    route: str = "Team Lead"
    gate: str = "auto"
    fields: list[ContractFieldDTO] = []


class ContractsDTO(CamelModel):
    emits: list[str]
    intakes: list[IntakeDTO]


class ContractsRequest(CamelModel):
    emits: list[str] = []
    intakes: list[IntakeDTO] = []


def _contracts_dto(frame: dict[str, Any]) -> ContractsDTO:
    intakes: list[IntakeDTO] = []
    for e in frame.get("intakes", []):
        if not isinstance(e, dict):
            continue
        name = str(e.get("handoff_type") or e.get("type") or "")
        intakes.append(
            IntakeDTO(
                id=str(e.get("id") or name),
                type=name,
                route=str(e.get("route") or "Team Lead"),
                gate=str(e.get("gate") or "auto"),
                fields=[
                    ContractFieldDTO(
                        name=str(f.get("name", "")),
                        type=str(f.get("type", "string")),
                        required=bool(f.get("required", False)),
                        example=f.get("example"),
                    )
                    for f in e.get("fields", [])
                    if isinstance(f, dict)
                ],
            )
        )
    return ContractsDTO(emits=department_emits(frame), intakes=intakes)


@router.get(
    "/departments/{department_id}/contracts",
    response_model=ContractsDTO,
    dependencies=[Depends(require_permission(perm(CONTRACT, VIEW)))],
)
async def read_contracts(
    department_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> ContractsDTO:
    dept = await db.get(m.Department, department_id)
    if dept is None or dept.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
    return _contracts_dto(dept.frame or {})


@router.put(
    "/departments/{department_id}/contracts",
    response_model=ContractsDTO,
    dependencies=[Depends(require_permission(perm(CONTRACT, MANAGE)))],
)
async def set_contracts(
    department_id: uuid.UUID,
    body: ContractsRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> ContractsDTO:
    dept = await db.get(m.Department, department_id)
    if dept is None or dept.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
    frame = dict(dept.frame or {})
    frame["emits"] = list(dict.fromkeys(body.emits))  # dedupe, preserve order
    frame["intakes"] = [
        {
            "id": it.id,
            "handoff_type": it.type,
            "route": it.route,
            "gate": it.gate,
            "fields": [f.model_dump(exclude_none=True) for f in it.fields],
        }
        for it in body.intakes
    ]
    dept.frame = frame  # reassign so SQLAlchemy flags the JSONB column dirty
    await db.flush()
    return _contracts_dto(frame)


@router.post(
    "/departments/{department_id}/emit",
    dependencies=[Depends(require_permission(perm(CONTRACT, MANAGE)))],
)
async def emit(
    department_id: uuid.UUID, body: EmitRequest, db: DbSession, principal: CurrentPrincipal
) -> dict[str, list[str]]:
    dept = await db.get(m.Department, department_id)
    if dept is None or dept.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
    actor = uuid.UUID(principal.subject) if _is_uuid(principal.subject) else uuid.uuid4()
    try:
        handoffs = await emit_event(
            db,
            tenant_id=principal.tenant_id,
            source_department=dept,
            event_type=body.event_type,
            payload=body.payload,
            created_by=actor,
        )
    except ContractViolation as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return {"handoffs": [str(h.id) for h in handoffs]}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
