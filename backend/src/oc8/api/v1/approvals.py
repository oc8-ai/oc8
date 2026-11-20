"""Approvals inbox decisions (HITL)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1._serializers import approval_to_dto
from oc8.approvals import (
    AlreadyDecided,
    EffectRefused,
    NotYourDepartment,
    NotYourSayAtAll,
    UnknownDecision,
    UnknownOption,
    decide_approval,
)
from oc8.approvals.repo import load_for_actor
from oc8.authz.permissions import APPROVAL_DECIDE
from oc8.authz.scope import HumanActor
from oc8.schemas.dto import ApprovalDTO
from oc8.schemas.requests import ApprovalDecisionRequest

router = APIRouter()

#: One sentence for "no such approval" and for "another department's approval",
#: because they must be indistinguishable. 409 carries the status and the time a
#: request was decided, and `approval.created` is still announced tenant-wide on
#: the realtime socket (§10 item 5) -- so a route that told the two apart would
#: let anyone holding ids off that socket ask which of them exist.
_NOT_FOUND = "approval not found"


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalDTO)
async def decide(
    approval_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(APPROVAL_DECIDE))],
) -> ApprovalDTO:
    # `load_for_actor`, never `db.get`: the load is the security boundary, and it
    # answers None for "not yours" as well as for "no such row".
    ar = await load_for_actor(db, approval_id, actor=actor)
    if ar is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    try:
        result = await decide_approval(
            db,
            ar,
            decision=body.decision,
            tenant_id=actor.principal.tenant_id,
            actor=actor,
            reason=body.reason,
            option=body.option,
        )
    except NotYourDepartment as exc:
        # The funnel's defence in depth, and unreachable while `load_for_actor`
        # above is the only load: a viewer seat can VIEW this row, so if this
        # route ever stops asking the repository first, this is what stops the
        # decision -- with the same sentence as a missing row, not a 403 that
        # confirms it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except NotYourSayAtAll as exc:
        # 403 and NOT the `_NOT_FOUND` 404, deliberately: this only ever escapes
        # after `may_decide` has already admitted the caller to this row, so the
        # row's existence is not news to them and naming the missing permission
        # tells them nothing they could not read off their own queue. A 404 here
        # would say "it vanished" about something still sitting in their list.
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except UnknownDecision as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown decision") from exc
    except AlreadyDecided as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except UnknownOption as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except EffectRefused as exc:
        # The decision could not take effect (the agent builder's name taken,
        # its department gone). Raising rolls the request back: the approval
        # stays pending and the approver reads why.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    dto = approval_to_dto(result.approval)
    # Only a tool_send decision suspends and resumes a run; the other action types
    # apply their effect directly, so they are never "resumed".
    dto.resumed = result.resumed_run_id is not None
    if result.resumed_run_id is not None:
        # Publish only after the re-queue is durably committed, so the worker
        # never reads a run whose QUEUED transition isn't visible yet.
        from oc8.runtime.intake import publish_run

        await db.commit()
        await publish_run(run_id=result.resumed_run_id, tenant_id=actor.principal.tenant_id)
    return dto
