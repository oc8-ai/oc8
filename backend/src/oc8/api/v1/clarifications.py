"""The other half of the workspace queue: questions an agent parked mid-run (§5).

There was no endpoint for these at all. A run that stopped to ask something sits
in `waiting_for_input`, and the one way to answer it was `POST /runs/{id}/answer`
-- gated on `run:control`, a permission in no seat vocabulary. So the person whose
answer the agent was waiting for was precisely the person who could not give it.

That admin door is left exactly as it is (§4). This is the departmental one
beside it, and every rule it obeys lives in `workspace/queue.py`: the department
comes from the agent, `may_decide` (not `may_view`) gates the answer, and "no such
question" and "not yours" are the same 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1._serializers import clarification_to_dto
from oc8.authz.permissions import CLARIFICATION_ANSWER, CLARIFICATION_VIEW
from oc8.authz.scope import HumanActor
from oc8.chat.questions import QuestionError
from oc8.runtime.clarification import AnswerRefused
from oc8.runtime.intake import publish_run
from oc8.schemas.dto import ClarificationAnswerDTO, ClarificationDTO
from oc8.schemas.requests import AnswerClarificationRequest
from oc8.workspace.queue import (
    DEFAULT_LIMIT,
    NotAnswerable,
    answer_clarification,
    open_clarifications,
)

router = APIRouter()

#: One sentence for "no such question" and for "another department's question".
#: Same reason `api/v1/approvals.py` gives one: an id that answers differently is
#: an id somebody can test, and `approval.created`/`run.status` are still
#: announced tenant-wide on the realtime socket (§10 item 5).
_NOT_FOUND = "clarification not found"


@router.get("/clarifications", response_model=list[ClarificationDTO])
async def list_clarifications(
    db: DbSession,
    # A ROUTE PARAMETER and not `dependencies=[...]`: the body needs the actor,
    # and a dependency in the decorator list is resolved and then thrown away.
    actor: Annotated[HumanActor, Depends(require_departmental(CLARIFICATION_VIEW))],
    status_filter: str = Query(default="open", alias="status"),
    limit: int = DEFAULT_LIMIT,
) -> list[ClarificationDTO]:
    """The open questions this caller may answer for, newest first.

    Only `status=open` is served, and anything else is refused rather than
    quietly answered with the open ones. There is no answered-questions queue: a
    parameter accepted and ignored is how a screen ends up showing a list that
    does not match its own filter, and nobody looks at the endpoint again.
    """
    if status_filter != "open":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "only status=open is served; there is no answered-questions queue",
        )
    rows = await open_clarifications(db, actor=actor, limit=limit)
    return [clarification_to_dto(r) for r in rows]


@router.post("/clarifications/{clarification_id}/answer", response_model=ClarificationAnswerDTO)
async def answer(
    clarification_id: uuid.UUID,
    body: AnswerClarificationRequest,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(CLARIFICATION_ANSWER))],
) -> ClarificationAnswerDTO:
    """Answer one parked question, and let the agent carry on.

    `publish_run` fires strictly AFTER the commit. A stream entry whose run row is
    not yet visible is a run a worker picks up and cannot find -- and the ids it
    needs are read out of the ORM object BEFORE the commit, because the commit
    unbinds `app.tenant_id` and any read after it comes back empty.
    """
    try:
        run = await answer_clarification(
            db, actor=actor, clarification_id=clarification_id, answer=body.answer
        )
    except NotAnswerable as exc:
        # 409, never 404: this only escapes once the scope has already admitted
        # the caller, so it may say why without telling anybody which rows exist.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except AnswerRefused as exc:
        # The secret gate every answer door shares (`resolve_clarification`).
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except QuestionError as exc:
        # A structured question (spec §4.1) whose written answer does not fit.
        # Raised before anything changed, so the question is still open.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)

    run_id, tenant_id = run.id, run.tenant_id
    await db.commit()
    await publish_run(run_id=run_id, tenant_id=tenant_id)
    return ClarificationAnswerDTO(
        id=str(clarification_id),
        run_id=str(run_id),
        status="answered",
        answer=body.answer,
    )
