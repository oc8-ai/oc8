"""Trigger an agent run (durable): create an AgentRun, enqueue it for the worker,
and return its queued status. Poll GET /runs/{id} for progress."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.versioning import pinned_version_no
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.authz.permissions import RUN, RUN_CONTROL, RUN_START, VIEW, perm
from oc8.runtime.clarification import resolve_clarification
from oc8.runtime.intake import enqueue_run, publish_run
from oc8.runtime.states import TERMINAL, RunState
from oc8.runtime.step_record import step_timing_dto
from oc8.schemas.dto import RunDTO, RunSummaryDTO, WorkspaceFileDTO, WorkspaceFilesDTO
from oc8.schemas.paging import Page
from oc8.schemas.requests import RunAgentRequest

router = APIRouter()
_SPILL_BASENAME = re.compile(r"^step-\d+-.*\.txt$")


class AnswerRequest(BaseModel):
    answer: str


class RunMessageRequest(BaseModel):
    body: str


#: `repr(exc)` of an exception the executor stored as `context["error"]`:
#: `RuntimeError('provider said no')` -> the message alone.
_EXC_REPR = re.compile(r"^[A-Za-z_][\w.]*\((['\"])(?P<message>.*)\1\)$", re.DOTALL)


def failure_reason(run: m.AgentRun) -> str | None:
    """Why a failed run failed, or None.

    A runtime that gives up on its own (the answer budget, a spent quota, a
    rate limit) records a sentence for the operator as the run's `output`; an
    exception that escaped the runtime is recorded as `error`, as its repr.
    The sentence wins, and the repr is cut down to its message.
    """
    if run.state != RunState.FAILED.value:
        return None
    ctx = run.context or {}
    output = ctx.get("output")
    if isinstance(output, str) and output.strip():
        return output.strip()
    error = ctx.get("error")
    if isinstance(error, str) and error.strip():
        match = _EXC_REPR.match(error.strip())
        return match.group("message") if match else error.strip()
    return None


def run_to_dto(run: m.AgentRun, *, agent_version_no: int | None = None) -> RunDTO:
    """`agent_version_no` is passed IN rather than resolved here.

    This function is synchronous and has five callers, three of which are write
    routes (`run`, `answer_run`, `cancel_run`) that return immediately after
    `db.commit()` -- at which point the transaction-local `app.tenant_id`
    binding is gone and any further query would silently return zero rows
    under RLS. Only `get_run` (here) and
    `get_session_run` (chat.py) read the number, via the shared
    `versioning.pinned_version_no` helper -- both serialize a `RunDTO` for the
    same frontend query-cache entry (`["run", runId]`), so both must fill this
    identically or whichever refetches last would blank out the other's value.
    """
    ctx = run.context or {}
    return RunDTO(
        id=str(run.id),
        agent_id=str(run.agent_id),
        state=run.state,
        phase=run.phase,
        output=ctx.get("output"),
        failure_reason=failure_reason(run),
        steps=int(ctx.get("steps", 0)),
        tool_calls=ctx.get("toolCalls", []),
        task_id=str(run.task_id) if run.task_id else None,
        question=ctx.get("pending_question"),
        rendered_components=ctx.get("rendered_components", []),
        todos=ctx.get("todos", []),
        agent_version_no=agent_version_no,
        updated_at=run.updated_at.isoformat(),
        step_timings=[step_timing_dto(e) for e in ctx.get("stepTimings", [])],
        connection_required=ctx.get("connection_required")
        if isinstance(ctx.get("connection_required"), dict)
        else None,
        tool_denied=ctx.get("tool_denied") if isinstance(ctx.get("tool_denied"), dict) else None,
    )


#: How many runs one list request may return. Deliberately small: a RunDTO
#: carries the run's whole toolCalls list and its stepTimings, so this is not
#: a cheap row -- and the two consumers (the My Work activity widget, the
#: needs-me queue) each show a handful.
_MAX_RUNS = 20


@router.get(
    "/runs",
    response_model=list[RunDTO],
    dependencies=[Depends(require_permission(perm(RUN, VIEW)))],
)
async def list_runs(
    db: DbSession,
    principal: CurrentPrincipal,
    state: str | None = None,
    limit: int = 10,
) -> list[RunDTO]:
    """This tenant's most recently touched runs, newest first.

    Why it exists: nothing else lists runs. The activity feed cannot stand in
    for it -- `activity_event` has no `run_id` column, and `AgentDTO` carries
    no run id either -- so "show me what my agents are doing, with the steps"
    had no query behind it at all.

    `state` is a comma-separated filter over `RunState` values; an unknown one
    is a 400 rather than a silent empty list, because a caller that misspells
    `waiting_for_aproval` would otherwise read the empty result as "nothing is
    parked". Omitted means every state.

    Scoping is RLS (tenant) plus `run:view`, the same pair that already gates
    `GET /runs/{id}`: this route deliberately does not add a departmental
    narrowing that the single-run route does not have, because two different
    answers to "may I see this run" is how one of them ends up wrong.
    """
    stmt = select(m.AgentRun).where(m.AgentRun.tenant_id == principal.tenant_id)
    if state:
        wanted = [s.strip() for s in state.split(",") if s.strip()]
        known = {s.value for s in RunState}
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"unknown run state(s): {', '.join(unknown)}"
            )
        stmt = stmt.where(m.AgentRun.state.in_(wanted))
    rows = (
        (
            await db.execute(
                stmt.order_by(m.AgentRun.updated_at.desc(), m.AgentRun.id.desc()).limit(
                    max(1, min(limit, _MAX_RUNS))
                )
            )
        )
        .scalars()
        .all()
    )
    return [run_to_dto(row) for row in rows]


#: How many history rows one page may hold. A summary is a small row, but the
#: tab shows one page at a time and twenty is already more than fits on screen.
_MAX_HISTORY = 50
#: Characters of task / answer a history row carries -- enough to recognise the
#: run, not to read it. The detail view fetches the whole run.
_EXCERPT_CHARS = 160


def _excerpt(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text if len(text) <= _EXCERPT_CHARS else text[: _EXCERPT_CHARS - 1].rstrip() + "…"


def run_to_summary(
    run: m.AgentRun, *, started_at: dt.datetime | None, finished_at: dt.datetime | None
) -> RunSummaryDTO:
    ctx = run.context or {}
    duration_ms = (
        int((finished_at - started_at).total_seconds() * 1000)
        if started_at is not None and finished_at is not None
        else None
    )
    return RunSummaryDTO(
        id=str(run.id),
        agent_id=str(run.agent_id),
        state=run.state,
        source=run.source,
        created_at=run.created_at.isoformat(),
        started_at=started_at.isoformat() if started_at else None,
        finished_at=finished_at.isoformat() if finished_at else None,
        duration_ms=duration_ms,
        steps=int(ctx.get("steps", 0)),
        task_excerpt=_excerpt(ctx.get("task")),
        output_excerpt=_excerpt(ctx.get("output")) if run.state != RunState.FAILED.value else None,
        failure_reason=failure_reason(run),
    )


@router.get(
    "/agents/{agent_id}/runs",
    response_model=Page[RunSummaryDTO],
    dependencies=[Depends(require_permission(perm(RUN, VIEW)))],
)
async def list_agent_runs(
    agent_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=_MAX_HISTORY)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RunSummaryDTO]:
    """One agent's runs, newest first, a page at a time (the History tab).

    Ordered by `created_at`, not `updated_at` like `GET /runs`: a history is
    read in the order things were started, and a heartbeat must not move a run
    up the list while somebody is paging through it.

    Same gate as `GET /runs/{id}` -- `run:view` plus the tenant -- for the same
    reason `list_runs` gives: two answers to "may I see this run" is how one of
    them ends up wrong. A deleted agent's history stays readable here; an agent
    of another tenant is a 404.
    """
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    base = select(m.AgentRun).where(
        m.AgentRun.tenant_id == principal.tenant_id, m.AgentRun.agent_id == agent_id
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = list(
        (
            await db.execute(
                base.order_by(m.AgentRun.created_at.desc(), m.AgentRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    # Start and end come from the transition log, not from created_at /
    # updated_at: a queued run waits before it starts, and updated_at moves on
    # every heartbeat and every later bookkeeping write (the evidence sweep).
    started: dict[uuid.UUID, dt.datetime] = {}
    finished: dict[uuid.UUID, dt.datetime] = {}
    terminal = [s.value for s in TERMINAL]
    if rows:
        tr = m.RunStateTransition
        spans = await db.execute(
            select(
                tr.run_id,
                func.min(tr.at).filter(tr.to_state == RunState.RUNNING.value),
                func.max(tr.at).filter(tr.to_state.in_(terminal)),
            )
            .where(tr.run_id.in_([r.id for r in rows]))
            .group_by(tr.run_id)
        )
        for run_id, first_running, last_terminal in spans.all():
            if first_running is not None:
                started[run_id] = first_running
            if last_terminal is not None:
                finished[run_id] = last_terminal

    return Page(
        items=[
            run_to_summary(
                r,
                started_at=started.get(r.id),
                # Only a run that is terminal NOW has an end.
                finished_at=finished.get(r.id) if r.state in terminal else None,
            )
            for r in rows
        ],
        total_count=int(total),
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
            if _SPILL_BASENAME.fullmatch(row.filename) is None
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
    return run_to_dto(run_row, agent_version_no=await pinned_version_no(db, run_row))


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
