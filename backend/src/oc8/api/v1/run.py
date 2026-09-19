"""Trigger an agent run (durable): create an AgentRun, enqueue it for the worker,
and return its queued status. Poll GET /runs/{id} for progress."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.authz.permissions import RUN, RUN_CONTROL, RUN_START, VIEW, perm
from oc8.runtime.clarification import resolve_clarification
from oc8.runtime.intake import enqueue_run, publish_run
from oc8.runtime.states import TERMINAL, RunState
from oc8.schemas.dto import RunDTO, WorkspaceFileDTO, WorkspaceFilesDTO
from oc8.schemas.requests import RunAgentRequest

router = APIRouter()


class AnswerRequest(BaseModel):
    answer: str


class RunMessageRequest(BaseModel):
    body: str


def run_to_dto(run: m.AgentRun) -> RunDTO:
    ctx = run.context or {}
    return RunDTO(
        id=str(run.id),
        agent_id=str(run.agent_id),
        state=run.state,
        phase=run.phase,
        output=ctx.get("output"),
        steps=int(ctx.get("steps", 0)),
        tool_calls=ctx.get("toolCalls", []),
        task_id=str(run.task_id) if run.task_id else None,
        question=ctx.get("pending_question"),
        rendered_components=ctx.get("rendered_components", []),
        todos=ctx.get("todos", []),
    )


@router.post(
    "/agents/{agent_id}/run",
    response_model=RunDTO,
    dependencies=[Depends(require_permission(RUN_START))],
)
async def run(
    agent_id: uuid.UUID,
    body: RunAgentRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> RunDTO:
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if agent.status == "pending_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, "agent awaiting hire approval")

    context: dict[str, object] = {"task": body.task}
    # Record the originating operator ONLY for an operator principal (§12.5 A3):
    # this route has no role gate, so a plugin/agent token could reach it, and its
    # subject must NOT be resolved as ("operator", subject) in the audit trail --
    # that mirrors resolve_responsible's own kind=="operator" guard on the live
    # principal. A non-operator run falls through to agent/tenant attribution.
    if principal.kind == "operator":
        context["originating_operator"] = principal.subject
    if body.mcp_connection_id is not None:
        context["mcp_connection_id"] = str(body.mcp_connection_id)

    # enqueue_run commits `db` before publishing; run_to_dto(run_row) below only
    # reads already-loaded attributes (safe because the sessionmaker uses
    # expire_on_commit=False).
    run_row, _published = await enqueue_run(
        db, tenant_id=principal.tenant_id, agent_id=agent.id, context=context, source="manual"
    )
    return run_to_dto(run_row)


@router.get(
    "/agents/{agent_id}/workspace/files",
    response_model=WorkspaceFilesDTO,
    dependencies=[Depends(require_permission(perm(RUN, VIEW)))],
)
async def list_workspace_files(
    agent_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> WorkspaceFilesDTO:
    """Every file any of this agent's runs has produced -- via the
    `write_output_file` control tool or a synced `/workspace/output/` mount
    (see `oc8.runtime.workspace`) -- newest first. Every runtime can produce
    these now, so unlike the old disk-based listing there is no more
    per-runtime "not applicable" case: an agent that has never produced one
    just gets an empty list. Content is downloaded via the shared
    `GET /files/{attachment_id}` route, not a route of its own here."""
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    rows = (
        (
            await db.execute(
                select(m.FileAttachment)
                .join(m.AgentRun, m.AgentRun.id == m.FileAttachment.owner_id)
                .where(
                    m.FileAttachment.tenant_id == principal.tenant_id,
                    m.FileAttachment.owner_type == "agent_run",
                    m.AgentRun.agent_id == agent_id,
                )
                .order_by(m.FileAttachment.created_at.desc(), m.FileAttachment.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return WorkspaceFilesDTO(
        files=[
            WorkspaceFileDTO(
                id=str(row.id),
                filename=row.filename,
                content_type=row.content_type,
                size_bytes=row.size_bytes,
                run_id=str(row.owner_id),
                created_at=row.created_at.isoformat(),
            )
            for row in rows
        ]
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunDTO,
    dependencies=[Depends(require_permission(perm(RUN, VIEW)))],
)
async def get_run(run_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> RunDTO:
    run_row = await db.get(m.AgentRun, run_id)
    if run_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run_to_dto(run_row)


@router.post(
    "/runs/{run_id}/answer",
    response_model=RunDTO,
    dependencies=[Depends(require_permission(RUN_CONTROL))],
)
async def answer_run(
    run_id: uuid.UUID,
    body: AnswerRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> RunDTO:
    run_row = await db.get(m.AgentRun, run_id)
    if run_row is None or run_row.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if run_row.state != RunState.WAITING_FOR_INPUT.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting for input")
    # Record the answer, fold it into the run context, and re-queue the
    # already-existing run (not a new one, so publish_run, not enqueue_run).
    await resolve_clarification(db, run=run_row, answer=body.answer)
    # No further DB query after commit (the transaction-local tenant binding
    # ends); run_to_dto only reads already-loaded attributes.
    await db.commit()
    await publish_run(run_id=run_row.id, tenant_id=principal.tenant_id)
    return run_to_dto(run_row)


@router.post(
    "/runs/{run_id}/message",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission(RUN_CONTROL))],
)
async def message_run(
    run_id: uuid.UUID,
    body: RunMessageRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, str]:
    """Send a message to a RUNNING agent (live steering). Recorded in the
    append-only run_message table; the executor injects it as a user turn at its
    next step boundary (READ COMMITTED), so the agent answers inline without the
    run being suspended. Distinct from /answer, which resolves an ask_user."""
    text = body.body.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must not be empty")
    run_row = await db.get(m.AgentRun, run_id)
    if run_row is None or run_row.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if run_row.state in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "run has already finished")
    db.add(
        m.RunMessage(
            tenant_id=principal.tenant_id,
            run_id=run_id,
            author=principal.subject or "operator",
            body=text,
        )
    )
    await db.commit()
    return {"status": "queued"}


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunDTO,
    dependencies=[Depends(require_permission(RUN_CONTROL))],
)
async def cancel_run(
    run_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> RunDTO:
    """Cancel a run cooperatively (§7.2, Paperclip A5). Records the operator's
    intent as a run_cancellation row; the executor (the sole agent_run.state
    mutator) applies it. This is deliberately NOT synchronous, so the returned
    DTO reports the run's CURRENT state, not `interrupted`:

    - queued  -> skipped before start on the worker's next pickup (agent freed then).
    - running -> stopped at the next step boundary, before the next LLM call.
    - waiting_for_input / waiting_for_approval -> a suspended run is not looping,
      so the cancel is recorded and honored only when the run is next resumed
      (via /answer or approval), which re-queues it into the before-start check.
      Until then the run stays suspended and its agent stays occupied. Callers
      must therefore inspect the returned `state` (and poll) rather than assume a
      200 means the run has already stopped. Immediately interrupting a suspended
      run is out of scope for this slice (queued + running); see the design spec's
      Non-Goals.

    409 if the run is already terminal; 404 if missing or cross-tenant (RLS).
    Idempotent: a second cancel adds no duplicate row.
    """
    run_row = await db.get(m.AgentRun, run_id)
    if run_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    current = RunState(run_row.state)
    if current in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, f"run already {current.value}")

    # Record the cancel on an UNLOCKED row. NEVER write agent_run here: a running
    # executor holds that row's lock for the whole run, so a write would block
    # until the run ends. The executor is the sole agent_run.state mutator and
    # honors this row -- skipping a queued run before start, or stopping a running
    # run at its next step boundary. Idempotent: a second cancel adds no row.
    existing = (
        await db.execute(select(m.RunCancellation).where(m.RunCancellation.run_id == run_id))
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            m.RunCancellation(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                requested_at=dt.datetime.now(tz=dt.UTC),
                cancellation_kind="operator_interrupted",
            )
        )

    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="run",
        action="run.cancel",
        resource={"run_id": str(run_id), "was_state": current.value},
        decision="allow",
        principal=principal,
    )
    await db.commit()
    return run_to_dto(run_row)
