"""Trigger an agent run (durable): create an AgentRun, enqueue it for the worker,
and return its queued status. Poll GET /runs/{id} for progress."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.repo import (
    RunReach,
    resolve_run_reach,
    restrict_to_run_reach,
    run_reachable_agent,
)
from oc8.agents.versioning import pinned_version_no
from oc8.api.deps import DbSession, require_departmental
from oc8.authz.permissions import RUN, RUN_CONTROL, RUN_START, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.chat.questions import QuestionError
from oc8.chat.service import record_answer_turn
from oc8.runtime.cancellation import record_operator_cancel
from oc8.runtime.clarification import AnswerRefused, resolve_clarification
from oc8.runtime.intake import enqueue_run, publish_run
from oc8.runtime.states import TERMINAL, RunState
from oc8.runtime.step_record import step_timing_dto
from oc8.schemas.dto import RunDTO, RunSummaryDTO, WorkspaceFileDTO, WorkspaceFilesDTO
from oc8.schemas.paging import Page
from oc8.schemas.requests import RunAgentRequest

router = APIRouter()
_SPILL_BASENAME = re.compile(r"^step-\d+-.*\.txt$")

#: The doors. `require_departmental` rather than `require_permission`: it admits
#: on the same tenant-wide hold (none of the three is a seat permission today)
#: and also hands the route body the caller's `DepartmentScope`, which
#: `resolve_run_reach` needs to narrow to the agents the caller may see.
_RunViewer = Annotated[HumanActor, Depends(require_departmental(perm(RUN, VIEW)))]
_RunStarter = Annotated[HumanActor, Depends(require_departmental(RUN_START))]
_RunController = Annotated[HumanActor, Depends(require_departmental(RUN_CONTROL))]


async def _reachable_run(
    db: DbSession, reach: RunReach, actor: HumanActor, run_id: uuid.UUID
) -> m.AgentRun:
    """One run whose agent `reach` admits, or a 404 -- the same 404 for "no such
    run" and "a run of an agent you may not see", so a run id cannot be used to
    learn that the run, or its agent, exists."""
    stmt = select(m.AgentRun).where(
        m.AgentRun.id == run_id, m.AgentRun.tenant_id == actor.principal.tenant_id
    )
    run_row = (
        await db.execute(
            restrict_to_run_reach(stmt, m.AgentRun.agent_id, reach).execution_options(
                populate_existing=True
            )
        )
    ).scalar_one_or_none()
    if run_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run_row


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
    )


#: How many runs one list request may return. Deliberately small: a RunDTO
#: carries the run's whole toolCalls list and its stepTimings, so this is not
#: a cheap row -- and the two consumers (the My Work activity widget, the
#: needs-me queue) each show a handful.
_MAX_RUNS = 20


@router.get("/runs", response_model=list[RunDTO])
async def list_runs(
    request: Request,
    db: DbSession,
    actor: _RunViewer,
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

    Scoping is RLS (tenant) plus `resolve_run_reach` -- the runs of agents the
    caller may see, with `run:view` -- the same rule `GET /runs/{id}` applies,
    because two different answers to "may I see this run" is how one of them
    ends up wrong. Narrowed in the statement, before the limit.
    """
    reach = await resolve_run_reach(request, db, actor, perm(RUN, VIEW))
    stmt = restrict_to_run_reach(
        select(m.AgentRun).where(m.AgentRun.tenant_id == actor.principal.tenant_id),
        m.AgentRun.agent_id,
        reach,
    )
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


@router.get("/agents/{agent_id}/runs", response_model=Page[RunSummaryDTO])
async def list_agent_runs(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: _RunViewer,
    limit: Annotated[int, Query(ge=1, le=_MAX_HISTORY)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RunSummaryDTO]:
    """One agent's runs, newest first, a page at a time (the History tab).

    Ordered by `created_at`, not `updated_at` like `GET /runs`: a history is
    read in the order things were started, and a heartbeat must not move a run
    up the list while somebody is paging through it.

    Same rule as `GET /runs/{id}` (`resolve_run_reach`) for the same reason
    `list_runs` gives: two answers to "may I see this run" is how one of them
    ends up wrong. An agent the caller may not see is a 404, exactly as
    `GET /agents/{id}` answers it. A deleted agent's history stays readable to
    a tenant-wide reader and to whoever may see its department; an agent of
    another tenant is a 404.
    """
    reach = await resolve_run_reach(request, db, actor, perm(RUN, VIEW))
    agent = await run_reachable_agent(
        db, reach=reach, tenant_id=actor.principal.tenant_id, agent_id=agent_id
    )
    if agent is None or agent.tenant_id != actor.principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    base = select(m.AgentRun).where(
        m.AgentRun.tenant_id == actor.principal.tenant_id, m.AgentRun.agent_id == agent_id
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


@router.post("/agents/{agent_id}/run", response_model=RunDTO)
async def run(
    agent_id: uuid.UUID,
    body: RunAgentRequest,
    request: Request,
    db: DbSession,
    actor: _RunStarter,
) -> RunDTO:
    principal = actor.principal
    reach = await resolve_run_reach(request, db, actor, RUN_START)
    agent = await run_reachable_agent(
        db, reach=reach, tenant_id=actor.principal.tenant_id, agent_id=agent_id
    )
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


@router.get("/agents/{agent_id}/workspace/files", response_model=WorkspaceFilesDTO)
async def list_workspace_files(
    agent_id: uuid.UUID, request: Request, db: DbSession, actor: _RunViewer
) -> WorkspaceFilesDTO:
    """Every file any of this agent's runs has produced -- via the
    `write_output_file` control tool or a synced `/workspace/output/` mount
    (see `oc8.runtime.workspace`) -- newest first. Every runtime can produce
    these now, so unlike the old disk-based listing there is no more
    per-runtime "not applicable" case: an agent that has never produced one
    just gets an empty list. Content is downloaded via the shared
    `GET /files/{attachment_id}` route, not a route of its own here.

    The files are run output, so they follow the run rule: an agent whose runs
    the caller may not see is a 404."""
    principal = actor.principal
    reach = await resolve_run_reach(request, db, actor, perm(RUN, VIEW))
    agent = await run_reachable_agent(
        db, reach=reach, tenant_id=actor.principal.tenant_id, agent_id=agent_id
    )
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


@router.get("/runs/{run_id}", response_model=RunDTO)
async def get_run(run_id: uuid.UUID, request: Request, db: DbSession, actor: _RunViewer) -> RunDTO:
    reach = await resolve_run_reach(request, db, actor, perm(RUN, VIEW))
    run_row = await _reachable_run(db, reach, actor, run_id)
    return run_to_dto(run_row, agent_version_no=await pinned_version_no(db, run_row))


@router.post("/runs/{run_id}/answer", response_model=RunDTO)
async def answer_run(
    run_id: uuid.UUID,
    body: AnswerRequest,
    request: Request,
    db: DbSession,
    actor: _RunController,
) -> RunDTO:
    principal = actor.principal
    # Run reach first: a run of an agent the caller may not reach is the same
    # 404 as no run at all, before any lock or answer is looked at.
    reach = await resolve_run_reach(request, db, actor, RUN_CONTROL)
    run_row = await _reachable_run(db, reach, actor, run_id)
    # The same lock the inbox and chat doors take, on the CLARIFICATION rows
    # (never the run, which a running executor holds): two answers to the same
    # question serialise, and the second finds it already answered.
    open_rows = list(
        (
            await db.execute(
                select(m.Clarification)
                .where(m.Clarification.run_id == run_row.id, m.Clarification.status == "open")
                .order_by(m.Clarification.created_at.desc())
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalars()
    )
    await db.refresh(run_row)
    if run_row.state != RunState.WAITING_FOR_INPUT.value or not open_rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting for input")
    # Record the answer, fold it into the run context, and re-queue the
    # already-existing run (not a new one, so publish_run, not enqueue_run).
    pending = (run_row.context or {}).get("pending_questions")
    try:
        structured = await resolve_clarification(db, run=run_row, answer=body.answer)
    except AnswerRefused as exc:
        # The secret gate every answer door shares: nothing was written.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except QuestionError as exc:
        # Structured questions (spec §4.1): the written answer must fit them.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    # A chat run's question answered here still shows in its chat (spec §4.1).
    await record_answer_turn(
        db,
        run=run_row,
        question=open_rows[0].question,
        pending=pending,
        structured=structured,
        text=body.answer,
        author_member_id=None,
        acting_subject=principal.subject,
        acting_role=principal.role,
    )
    # Built BEFORE the commit: the commit expires the row, and reading it
    # afterwards is IO outside the request's greenlet (MissingGreenlet) -- and
    # would run without the transaction-local tenant binding anyway.
    # `updated_at` is server-set on the flush, so reload it first.
    await db.refresh(run_row)
    dto = run_to_dto(run_row)
    run_id = run_row.id
    await db.commit()
    await publish_run(run_id=run_id, tenant_id=principal.tenant_id)
    return dto


@router.post("/runs/{run_id}/message", status_code=status.HTTP_202_ACCEPTED)
async def message_run(
    run_id: uuid.UUID,
    body: RunMessageRequest,
    request: Request,
    db: DbSession,
    actor: _RunController,
) -> dict[str, str]:
    """Send a message to a RUNNING agent (live steering). Recorded in the
    append-only run_message table; the executor injects it as a user turn at its
    next step boundary (READ COMMITTED), so the agent answers inline without the
    run being suspended. Distinct from /answer, which resolves an ask_user."""
    text = body.body.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must not be empty")
    principal = actor.principal
    reach = await resolve_run_reach(request, db, actor, RUN_CONTROL)
    run_row = await _reachable_run(db, reach, actor, run_id)
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


@router.post("/runs/{run_id}/cancel", response_model=RunDTO)
async def cancel_run(
    run_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: _RunController,
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

    409 if the run is already terminal; 404 if missing, cross-tenant (RLS), or
    a run of an agent the caller may not see.
    Idempotent: a second cancel adds no duplicate row.
    """
    principal = actor.principal
    reach = await resolve_run_reach(request, db, actor, RUN_CONTROL)
    run_row = await _reachable_run(db, reach, actor, run_id)

    current = RunState(run_row.state)
    if current in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, f"run already {current.value}")

    # The same unlocked run_cancellation row the chat's Stop writes; see
    # oc8.runtime.cancellation.
    await record_operator_cancel(db, run=run_row, principal=principal, actor_id=None)
    await db.commit()
    return run_to_dto(run_row)
