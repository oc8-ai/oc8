"""Writing to and stopping a running run from the chat (spec §3.2).

A member-facing door beside the admin `/runs/{id}/message` and `/cancel`
(both `run:control`, unchanged): the gate here is the run's own authority --
its originating operator, or the agent's managers for an autonomous run
(`may_steer_run`). Stop uses exactly the same rule as writing, so this door
never lets anyone stop a run they could not also steer. "Not yours" and "no
such run" are the same 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from oc8 import models as m
from oc8.agents.versioning import resolve_version
from oc8.api.deps import DbSession, require_departmental
from oc8.authz.authority import authority_for_principal
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.runtime.registry import RuntimeResolutionError, resolve_runtime
from oc8.runtime.run_inputs import (
    InputRefused,
    add_run_input,
    may_steer_run,
    request_stop,
    runtime_accepts_inputs,
)
from oc8.runtime.states import TERMINAL, RunState
from oc8.schemas.dto import RunInputDTO, RunSteeringDTO
from oc8.schemas.requests import RunInputRequest

router = APIRouter()

_Actor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]
_NOT_FOUND = "run not found"
_NO_INPUTS = "This run doesn't take messages — you can stop it."


async def _steerable(
    request: Request, run_id: uuid.UUID, db: DbSession, actor: HumanActor
) -> m.AgentRun:
    """The run, read fresh, if this caller may steer it -- by the request's
    own authority and seat scope, never a cached one."""
    run = (
        await db.execute(
            select(m.AgentRun)
            .where(m.AgentRun.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.tenant_id != actor.principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    authority = await authority_for_principal(request, db, actor.principal)
    if not await may_steer_run(
        db, run=run, member=actor.member, authority=authority, scope=actor.scope
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return run


async def _accepts(db: DbSession, run: m.AgentRun) -> bool:
    """By the run's PINNED runtime, the one actually executing it."""
    agent = await db.get(m.Agent, run.agent_id)
    if agent is None:
        return False
    pinned = await resolve_version(db, run, agent)
    try:
        adapter = await resolve_runtime(
            db, tenant_id=run.tenant_id, agent=agent, runtime_ref=pinned["runtime_ref"]
        )
    except RuntimeResolutionError:
        return False
    return runtime_accepts_inputs(adapter)


def _is_terminal(run: m.AgentRun) -> bool:
    return RunState(run.state) in TERMINAL


def _input_dto(row: m.RunInput, author_name: str) -> RunInputDTO:
    return RunInputDTO(
        id=str(row.id),
        text=row.text,
        author_name=author_name,
        created_at=row.created_at.isoformat(),
        consumed_at=row.consumed_at.isoformat() if row.consumed_at else None,
        consumed_step=row.consumed_step,
    )


def _name(member: m.OrgMember | None) -> str:
    return (member.display_name or member.subject) if member is not None else "a colleague"


@router.get("/runs/{run_id}/steering", response_model=RunSteeringDTO)
async def get_steering(
    request: Request, run_id: uuid.UUID, db: DbSession, actor: _Actor
) -> RunSteeringDTO:
    run = await _steerable(request, run_id, db, actor)
    rows = list(
        (
            await db.execute(
                select(m.RunInput).where(m.RunInput.run_id == run.id).order_by(m.RunInput.seq)
            )
        ).scalars()
    )
    names: dict[uuid.UUID, str] = {}
    for row in rows:
        if row.author_member_id not in names:
            names[row.author_member_id] = _name(await db.get(m.OrgMember, row.author_member_id))
    return RunSteeringDTO(
        may_write=not _is_terminal(run),
        accepts_inputs=await _accepts(db, run),
        inputs=[_input_dto(row, names[row.author_member_id]) for row in rows],
    )


@router.post(
    "/runs/{run_id}/inputs", response_model=RunInputDTO, status_code=status.HTTP_201_CREATED
)
async def post_input(
    request: Request, run_id: uuid.UUID, body: RunInputRequest, db: DbSession, actor: _Actor
) -> RunInputDTO:
    run = await _steerable(request, run_id, db, actor)
    if not _is_terminal(run) and not await _accepts(db, run):
        raise HTTPException(status.HTTP_409_CONFLICT, _NO_INPUTS)
    try:
        row = await add_run_input(db, run=run, member=actor.member, text=body.text)
    except InputRefused as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    await db.refresh(row)
    dto = _input_dto(row, _name(actor.member))
    await db.commit()
    return dto


@router.post("/runs/{run_id}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_run(
    request: Request, run_id: uuid.UUID, db: DbSession, actor: _Actor
) -> dict[str, str]:
    """Cooperative, like the admin cancel: the run stops at its next step
    boundary, so the answer is the run's CURRENT state, not `interrupted`."""
    run = await _steerable(request, run_id, db, actor)
    if _is_terminal(run):
        raise HTTPException(status.HTTP_409_CONFLICT, "run has already finished")
    state = run.state
    await request_stop(db, run=run, actor_member_id=actor.member.id, principal=actor.principal)
    await db.commit()
    return {"state": state}
