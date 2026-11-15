"""API for data boards (dashboards): list, open, query and delete.

Visibility is `agent:view` in the board's department (spec §6.8.2): every seat
carries it and tenant-wide holders see all boards. Both resolve through
`boards.repo`, the same rule that shows the caller the producing agent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import (
    DbSession,
    authorize_agent_write,
    require_agent_write,
    require_departmental,
)
from oc8.audit import append_event
from oc8.authz.authority import Authority, authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.boards.lifecycle import (
    count_board_records,
    lock_live_board,
    remove_board,
)
from oc8.boards.query import BoardQuery, QueryInvalid, QueryResult, run_query
from oc8.boards.repo import may_delete_board, visible_board, visible_boards
from oc8.schemas.dto import BoardSummaryDTO, DataBoardDTO

router = APIRouter()


async def _summaries(
    db: AsyncSession, boards: list[m.DataBoard], *, authority: Authority, actor: HumanActor
) -> list[BoardSummaryDTO]:
    if not boards:
        return []
    ids = [b.id for b in boards]
    stats = {
        row.board_id: (int(row.n), row.last)
        for row in (
            await db.execute(
                select(
                    m.DataBoardBatch.board_id,
                    func.sum(m.DataBoardBatch.row_count).label("n"),
                    func.max(m.DataBoardBatch.recorded_at).label("last"),
                )
                .where(m.DataBoardBatch.board_id.in_(ids))
                .group_by(m.DataBoardBatch.board_id)
            )
        ).all()
    }
    out: list[BoardSummaryDTO] = []
    for b in boards:
        # Only after the repository admitted the board: name and deleted_at only.
        agent = await db.get(m.Agent, b.agent_id) if b.agent_id is not None else None
        count, last = stats.get(b.id, (0, None))
        out.append(
            BoardSummaryDTO(
                id=str(b.id),
                title=b.title,
                description=b.description,
                department_id=str(b.department_id),
                agent_id=str(b.agent_id) if b.agent_id is not None else None,
                agent_name=agent.name if agent is not None else None,
                stale=agent is None or agent.deleted_at is not None,
                record_count=count,
                last_recorded_at=last.isoformat() if last is not None else None,
                may_delete=may_delete_board(authority, actor.scope, b),
            )
        )
    return out


async def _admitted(
    request: Request, db: AsyncSession, actor: HumanActor, board_id: uuid.UUID
) -> tuple[m.DataBoard, Authority]:
    authority = await authority_for_principal(request, db, actor.principal)
    board = await visible_board(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide_read(authority, perm(AGENT, VIEW)),
        tenant_id=actor.principal.tenant_id,
        board_id=board_id,
    )
    if board is None:
        raise HTTPException(404, "board not found")
    if board.removed_at is not None:
        raise HTTPException(410, "board removed")
    return board, authority


@router.get("/boards", response_model=list[BoardSummaryDTO])
async def list_boards(
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
    agent_id: Annotated[uuid.UUID | None, Query(alias="agentId")] = None,
) -> list[BoardSummaryDTO]:
    authority = await authority_for_principal(request, db, actor.principal)
    boards = await visible_boards(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide_read(authority, perm(AGENT, VIEW)),
        tenant_id=actor.principal.tenant_id,
        agent_id=agent_id,
    )
    return await _summaries(db, boards, authority=authority, actor=actor)


@router.get("/boards/{board_id}", response_model=DataBoardDTO)
async def get_board(
    board_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
) -> DataBoardDTO:
    board, authority = await _admitted(request, db, actor, board_id)
    (summary,) = await _summaries(db, [board], authority=authority, actor=actor)
    return DataBoardDTO(**summary.model_dump(), spec=board.spec, spec_version=board.spec_version)


@router.post("/boards/{board_id}/query", response_model=QueryResult)
async def query_board(
    board_id: uuid.UUID,
    body: BoardQuery,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_departmental(perm(AGENT, VIEW)))],
) -> QueryResult:
    board, _authority = await _admitted(request, db, actor, board_id)
    try:
        return await run_query(db, board=board, query=body)
    except QueryInvalid as exc:
        raise HTTPException(422, f"board_query:{exc.code}") from exc


@router.delete("/boards/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(
    board_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
    records: Annotated[int, Query(ge=0)],
) -> Response:
    """Remove a board and its history. `records` is the count the person was shown;
    if it moved since, nothing is deleted (409) so the confirmation stays honest."""
    not_found = HTTPException(404, "board not found")
    authority = await authority_for_principal(request, db, actor.principal)
    board = await visible_board(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide_read(authority, perm(AGENT, VIEW)),
        tenant_id=actor.principal.tenant_id,
        board_id=board_id,
    )
    if board is None or board.removed_at is not None:
        raise not_found
    await authorize_agent_write(request, db, actor, board.department_id, not_found=not_found)
    # Lock before counting: writers serialize against this delete.
    locked = await lock_live_board(db, board.id)
    if locked is None:
        raise not_found
    board = locked
    if await count_board_records(db, board.id) != records:
        raise HTTPException(409, "board_records_changed")
    department_id = board.department_id
    deleted = await remove_board(db, board, now=dt.datetime.now(tz=dt.UTC))
    await append_event(
        db,
        tenant_id=actor.principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="board.deleted",
        resource={
            "board_id": str(board_id),
            "record_count": deleted,
            "department_id": str(department_id),
            "by": actor.principal.subject,
        },
        principal=actor.principal,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
