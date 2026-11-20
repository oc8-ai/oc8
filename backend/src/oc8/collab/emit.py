"""Emit a business event -> resolve contract bindings -> create handoffs (§14a.3)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.collab.contracts import (
    ContractViolation,
    apply_payload_map,
    department_emits,
    department_intake,
)
from oc8.collab.handoff import create_handoff


async def emit_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_department: m.Department,
    event_type: str,
    payload: dict[str, Any],
    created_by: uuid.UUID,
) -> list[m.Handoff]:
    if event_type not in department_emits(source_department.frame):
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="agent",
            actor_id=created_by,
            category="contract",
            action=f"emit:{event_type}",
            resource={"department_id": str(source_department.id)},
            decision="deny",
            reason="event not declared in department emits",
        )
        raise ContractViolation(f"department does not emit {event_type}")

    bindings = (
        (
            await db.execute(
                select(m.ContractBinding).where(
                    m.ContractBinding.event_type == event_type,
                    m.ContractBinding.source_department_id == source_department.id,
                )
            )
        )
        .scalars()
        .all()
    )

    created: list[m.Handoff] = []
    for binding in bindings:
        target = await db.get(m.Department, binding.target_department_id)
        handoff_type = await db.get(m.HandoffType, binding.handoff_type_id)
        # An archived department takes no new work.
        if target is None or target.deleted_at is not None or handoff_type is None:
            continue
        if department_intake(target.frame, handoff_type.name) is None:
            await append_event(
                db,
                tenant_id=tenant_id,
                actor_type="agent",
                actor_id=created_by,
                category="contract",
                action=f"intake:{handoff_type.name}",
                resource={"target_department_id": str(target.id)},
                decision="deny",
                reason="target department does not intake this handoff type",
            )
            continue
        mapped = apply_payload_map(binding.payload_map, payload)
        handoff = await create_handoff(
            db,
            tenant_id=tenant_id,
            handoff_type=handoff_type,
            source_department_id=source_department.id,
            target_department_id=target.id,
            payload=mapped,
            created_by=created_by,
            gate=binding.gate,
        )
        created.append(handoff)
    return created
