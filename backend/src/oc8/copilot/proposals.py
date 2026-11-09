"""Proposal creation and single-use application."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.audit import append_event
from oc8.auth import Principal
from oc8.authz.scope import scope_for_principal
from oc8.copilot.capabilities import (
    InvalidOperation,
    apply_operation,
    operation_data,
    operation_label,
    operation_references,
    parse_operations,
    target_revision,
)
from oc8.copilot.models import CopilotOperation, CopilotProposal


class ProposalRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("copilot proposal cannot be applied")


class ProposalNotRejectable(ValueError):
    def __init__(self) -> None:
        super().__init__("copilot proposal cannot be rejected")


@dataclass(frozen=True)
class ApplyResult:
    proposal_id: uuid.UUID
    status: str


async def create_proposal(
    db: AsyncSession,
    actor: Principal,
    operations: object,
    *,
    idempotency_key: uuid.UUID | None = None,
) -> CopilotProposal:
    parsed = parse_operations(operations)
    proposal = CopilotProposal(tenant_id=actor.tenant_id, created_by=actor.subject,
                               idempotency_key=idempotency_key or uuid.uuid4())
    db.add(proposal)
    await db.flush()
    for ordinal, operation in enumerate(parsed):
        db.add(CopilotOperation(tenant_id=actor.tenant_id, proposal_id=proposal.id, ordinal=ordinal,
                                 operation_type=operation_label(operation.type),
                                 configuration=operation_data(operation),
                                 target_revision=await target_revision(db, operation)))
    await db.flush()
    # From the actor, not hardcoded: an agent (the tenant Assistant) can create a
    # proposal now, and an audit line that calls that an operator is simply
    # false. `Principal.kind` is the same vocabulary actor_type already uses.
    await append_event(db, tenant_id=actor.tenant_id, actor_type=actor.kind, actor_id=None,
                       category="copilot", action="proposal.created",
                       resource={"proposal_id": str(proposal.id), "operation_count": len(parsed)},
                       principal=actor)
    return proposal


async def _is_stale(db: AsyncSession, operation: CopilotOperation) -> bool:
    parsed = parse_operations([operation.configuration])[0]
    try:
        return operation.target_revision != await target_revision(db, parsed, lock_for_apply=True)
    except InvalidOperation:
        return True


async def reject_proposal(
    db: AsyncSession, proposal_id: uuid.UUID, actor: Principal
) -> CopilotProposal:
    """Reject a draft without calling any capability applier."""
    proposal = await db.get(CopilotProposal, proposal_id)
    if proposal is None or proposal.status != "draft":
        raise ProposalNotRejectable()
    proposal.status = "rejected"
    proposal.applied_by = actor.subject
    proposal.revision += 1
    await db.flush()
    await append_event(
        db,
        tenant_id=actor.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="copilot",
        action="proposal.rejected",
        resource={"proposal_id": str(proposal.id)},
        principal=actor,
    )
    return proposal


async def apply_proposal(db: AsyncSession, proposal_id: uuid.UUID, actor: Principal) -> ApplyResult:
    proposal = await db.get(CopilotProposal, proposal_id)
    if proposal is None or proposal.status != "draft":
        raise ProposalRejected()
    operations = (await db.execute(select(CopilotOperation).where(
        CopilotOperation.proposal_id == proposal.id).order_by(CopilotOperation.ordinal)
    )).scalars().all()
    stale = [await _is_stale(db, operation) for operation in operations]
    if not operations or any(stale):
        proposal.status = "expired"
        await db.flush()
        raise ProposalRejected()
    try:
        actor_member, _scope = await scope_for_principal(db, actor, upsert=True)
    except PermissionError:
        actor_member = None
    actor_member_id = actor_member.id if actor_member is not None else None
    try:
        for operation in operations:
            await apply_operation(
                db,
                tenant_id=actor.tenant_id,
                data=operation.configuration,
                actor_member_id=actor_member_id,
            )
    except InvalidOperation as exc:
        proposal.status = "rejected"
        await db.flush()
        raise ProposalRejected() from exc
    proposal.status = "applied"
    proposal.applied_by = actor.subject
    proposal.revision += 1
    await db.flush()
    await append_event(db, tenant_id=actor.tenant_id, actor_type="operator", actor_id=None,
                       category="copilot", action="proposal.applied",
                       resource={
                           "proposal_id": str(proposal.id),
                           "operations": [
                               {
                                   "label": operation.operation_type,
                                   "references": operation_references(operation.configuration),
                               }
                               for operation in operations
                           ],
                       }, principal=actor)
    return ApplyResult(proposal.id, proposal.status)
