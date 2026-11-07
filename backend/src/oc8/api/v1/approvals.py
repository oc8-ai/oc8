"""Approvals inbox decisions (HITL)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from oc8.api.deps import DbSession, holds_departmental, require_departmental
from oc8.api.v1._serializers import approval_to_dto
from oc8.approvals import (
    AlreadyDecided,
    NotYourApproval,
    NotYourDepartment,
    NotYourSayAtAll,
    UnknownDecision,
    UnknownOption,
    decide_approval,
)
from oc8.approvals.repo import load_for_actor, next_stage_statuses
from oc8.authz.permissions import APPROVAL_DECIDE, COPILOT_USE
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
    request: Request,
    db: DbSession,
    # `or_tenant_wide=COPILOT_USE`: a member with no seat must be able to answer
    # the approval their own guardrail routed to them (D3a). Everything else is
    # answered below exactly as the narrower gate answered it.
    actor: Annotated[
        HumanActor, Depends(require_departmental(APPROVAL_DECIDE, or_tenant_wide=COPILOT_USE))
    ],
) -> ApprovalDTO:
    # `load_for_actor`, never `db.get`: the load is the security boundary, and it
    # answers None for "not yours" as well as for "no such row".
    ar = await load_for_actor(db, approval_id, actor=actor)
    if not await holds_departmental(request, db, actor, APPROVAL_DECIDE):
        # Admitted only to answer their own rows. Anything else -- missing,
        # someone else's, a department row they can see but not decide -- gets
        # the refusal the gate used to give, the same for all three, so this
        # branch is no oracle either.
        if ar is None or ar.assignee_member_id != actor.member.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"requires permission: {APPROVAL_DECIDE}"
            )
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
    except (NotYourDepartment, NotYourApproval) as exc:
        # The funnel's defence in depth, and unreachable while `load_for_actor`
        # above is the only load: a viewer seat can VIEW this row, so if this
        # route ever stops asking the repository first, this is what stops the
        # decision -- with the same sentence as a missing row, not a 403 that
        # confirms it exists. Likewise a row routed to another member (D3a),
        # which `load_for_actor` already hides from everyone but its assignee.
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

    dto = approval_to_dto(result.approval)
    if result.next_stage_id is not None:
        # The member's yes opened (and maybe, for a member who approves for the
        # department too, also answered) the department stage: say how it stands.
        dto.next_stage_status = (await next_stage_statuses(db, {result.approval.id})).get(
            result.approval.id
        )
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
