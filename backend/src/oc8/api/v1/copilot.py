"""Human-reviewed, secret-blind Copilot proposal API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import COPILOT, MANAGE, VIEW, perm
from oc8.copilot.capabilities import InvalidOperation, operation_references
from oc8.copilot.models import CopilotOperation, CopilotProposal
from oc8.copilot.proposals import (
    ProposalNotRejectable,
    ProposalRejected,
    apply_proposal,
    create_proposal,
    reject_proposal,
)

router = APIRouter()


def _safe_invalid() -> HTTPException:
    # Do not attach Pydantic/JSON parser details: those can quote submitted input.
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid copilot proposal")


async def _response(db: DbSession, proposal: CopilotProposal) -> dict[str, Any]:
    operations = (await db.execute(select(CopilotOperation).where(
        CopilotOperation.proposal_id == proposal.id
    ).order_by(CopilotOperation.ordinal))).scalars().all()
    return {
        "id": str(proposal.id), "status": proposal.status, "revision": proposal.revision,
        "operations": [
            {
                "label": operation.operation_type,
                "references": operation_references(operation.configuration),
            }
            for operation in operations
        ],
    }


@router.post("/copilot/proposals", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission(perm(COPILOT, MANAGE)))])
async def propose(request: Request, db: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"operations", "idempotencyKey"}:
            raise InvalidOperation()
        key = body.get("idempotencyKey")
        proposal = await create_proposal(
            db, principal, body.get("operations"),
            idempotency_key=uuid.UUID(key) if key is not None else None,
        )
    except (InvalidOperation, ValueError, TypeError):
        raise _safe_invalid() from None
    return await _response(db, proposal)


@router.get("/copilot/proposals",
            dependencies=[Depends(require_permission(perm(COPILOT, VIEW)))])
async def list_proposals(
    db: DbSession, principal: CurrentPrincipal, status_filter: str = "draft"
) -> list[dict[str, Any]]:
    """Every proposal in this tenant with a given status, newest first.

    Exists so the Copilot dock can show what is waiting without parsing a
    proposal id out of a free-text chat message: `propose_change` answers the
    model with a sentence, and the sentence is not an API. Listing by status
    also covers proposals raised over Telegram, which no web transcript
    mentions at all.

    `draft` by default -- that is the only status `create_proposal` ever writes
    (see `control_tools.py`'s propose_change branch: it may DRAFT and nothing
    else), so "draft" is this system's word for "waiting for a human".
    """
    rows = (
        await db.execute(
            select(CopilotProposal)
            .where(
                CopilotProposal.tenant_id == principal.tenant_id,
                CopilotProposal.status == status_filter,
                CopilotProposal.kind == "operations",
            )
            .order_by(CopilotProposal.created_at.desc())
        )
    ).scalars().all()
    return [await _response(db, row) for row in rows]


@router.get("/copilot/proposals/{proposal_id}",
            dependencies=[Depends(require_permission(perm(COPILOT, VIEW)))])
async def review(proposal_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    proposal = await db.get(CopilotProposal, proposal_id)
    # A builder proposal is its owner's chat content, read through its own
    # route (Ruling A10): here it does not exist.
    if proposal is None or proposal.kind != "operations":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    return await _response(db, proposal)


@router.post("/copilot/proposals/{proposal_id}/apply",
             dependencies=[Depends(require_permission(perm(COPILOT, MANAGE)))])
async def apply(
    proposal_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        await apply_proposal(db, proposal_id, principal)
    except ProposalRejected:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "copilot proposal cannot be applied"
        ) from None
    proposal = await db.get(CopilotProposal, proposal_id)
    assert proposal is not None
    return await _response(db, proposal)


@router.post(
    "/copilot/proposals/{proposal_id}/reject",
    dependencies=[Depends(require_permission(perm(COPILOT, MANAGE)))],
)
async def reject(
    proposal_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        proposal = await reject_proposal(db, proposal_id, principal)
    except ProposalNotRejectable:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "copilot proposal cannot be rejected"
        ) from None
    return await _response(db, proposal)
