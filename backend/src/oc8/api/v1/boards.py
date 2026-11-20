"""API for data boards (dashboards): list, open, query, delete and share.

Visibility is `agent:view` in the board's department (spec §6.8.2): every seat
carries it and tenant-wide holders see all boards. Both resolve through
`boards.repo`, the same rule that shows the caller the producing agent -- plus,
for the person asking (`member_id`), the boards shared with them.

The read and share routes sit behind `require_departmental(agent:view,
or_board_share=True)`: a member with no seat but a board shared to them by name
must reach it, and every row is still narrowed by `boards.repo`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.repo import agent_runnable_in, run_reach
from oc8.api.deps import (
    DbSession,
    authorize_agent_write,
    require_agent_write,
    require_departmental,
)
from oc8.audit import append_event
from oc8.authz.authority import Authority, authority_for_principal, tenant_wide_read
from oc8.authz.permissions import AGENT, RUN_START, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.boards.lifecycle import (
    count_board_records,
    lock_live_board,
    remove_board,
)
from oc8.boards.query import BoardQuery, QueryInvalid, QueryResult, run_query
from oc8.boards.repo import may_delete_board, visible_board, visible_boards
from oc8.boards.sharing import ShareNotFound, ShareTargetInvalid, share_board, unshare_board
from oc8.schemas.dto import (
    BoardShareDTO,
    BoardSummaryDTO,
    DataBoardDTO,
    ShareTargetDTO,
    ShareTargetsDTO,
)
from oc8.schemas.requests import BoardShareRequest

router = APIRouter()

#: The board read gate: a seat with `agent:view`, `agent:view` tenant-wide, or a
#: board shared with the caller by name (`require_departmental`'s docstring).
BoardReader = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_board_share=True))
]


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
    # Only after the repository admitted the boards: name, deleted_at and what
    # the run door reads, never more of the agent.
    agent_ids = {b.agent_id for b in boards if b.agent_id is not None}
    agents = (
        {
            a.id: a
            for a in (await db.execute(select(m.Agent).where(m.Agent.id.in_(agent_ids))))
            .scalars()
            .all()
        }
        if agent_ids
        else {}
    )
    reach = run_reach(authority=authority, scope=actor.scope, permission=RUN_START)
    tenant_wide = tenant_wide_read(authority, perm(AGENT, VIEW))
    out: list[BoardSummaryDTO] = []
    for b in boards:
        may_delete = may_delete_board(authority, actor.scope, b)
        agent = agents.get(b.agent_id) if b.agent_id is not None else None
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
                may_delete=may_delete,
                may_run=agent_runnable_in(reach, agent),
                may_share=may_delete,
                # Admitted, but not by the caller's own reach: only a share.
                shared_with_me=not tenant_wide and b.department_id not in actor.scope.viewable,
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
        member_id=actor.member.id,
    )
    if board is None:
        raise HTTPException(404, "board not found")
    if board.removed_at is not None:
        raise HTTPException(410, "board removed")
    return board, authority


async def _sharer(
    request: Request, db: AsyncSession, actor: HumanActor, board_id: uuid.UUID
) -> m.DataBoard:
    """404 when the caller cannot see the board, 403 when they see it but may
    not delete it -- who may share is who may delete (spec "Who may share")."""
    board, authority = await _admitted(request, db, actor, board_id)
    if not may_delete_board(authority, actor.scope, board):
        raise HTTPException(403, "requires agent:manage in the board's department")
    return board


@router.get("/boards", response_model=list[BoardSummaryDTO])
async def list_boards(
    request: Request,
    db: DbSession,
    actor: BoardReader,
    agent_id: Annotated[uuid.UUID | None, Query(alias="agentId")] = None,
) -> list[BoardSummaryDTO]:
    authority = await authority_for_principal(request, db, actor.principal)
    boards = await visible_boards(
        db,
        scope=actor.scope,
        tenant_wide=tenant_wide_read(authority, perm(AGENT, VIEW)),
        tenant_id=actor.principal.tenant_id,
        agent_id=agent_id,
        member_id=actor.member.id,
    )
    return await _summaries(db, boards, authority=authority, actor=actor)


@router.get("/boards/{board_id}", response_model=DataBoardDTO)
async def get_board(
    board_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: BoardReader,
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
    actor: BoardReader,
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
    # Lock before counting: writers serialize against this delete. Read again
    # under the lock: removed or archived since the read above is not found.
    locked = await lock_live_board(db, board.id, archived=False)
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


# --- sharing ----------------------------------------------------------------

_TARGET_LIMIT = 20


def _share_dto(
    share: m.DataBoardShare,
    members: dict[uuid.UUID, m.OrgMember],
    departments: dict[uuid.UUID, m.Department],
) -> BoardShareDTO:
    member = members.get(share.member_id) if share.member_id is not None else None
    dept = departments.get(share.department_id) if share.department_id is not None else None
    return BoardShareDTO(
        id=str(share.id),
        member_id=str(share.member_id) if share.member_id is not None else None,
        member_name=_member_name(member) if member is not None else None,
        department_id=str(share.department_id) if share.department_id is not None else None,
        department_name=dept.name if dept is not None else None,
        created_at=share.created_at.isoformat(),
    )


def _member_name(member: m.OrgMember, *, typed_email: str | None = None) -> str:
    """A member's label for a sharer, who may lack `member:view`: the display
    name, else a masked address (`j…@example.com`). The full email only for the
    member found by the exact address the caller typed -- nothing they did not
    already know. Never the subject."""
    if member.display_name:
        return member.display_name
    if member.email and typed_email and member.email.lower() == typed_email.lower():
        return member.email
    return _masked(member.email or member.subject)


def _masked(address: str) -> str:
    local, at, domain = address.partition("@")
    if not at or not local or not domain:
        return "…"
    return f"{local[0]}…@{domain}"


async def _share_dtos(db: AsyncSession, shares: list[m.DataBoardShare]) -> list[BoardShareDTO]:
    member_ids = {s.member_id for s in shares if s.member_id is not None}
    dept_ids = {s.department_id for s in shares if s.department_id is not None}
    members = (
        {
            r.id: r
            for r in (await db.execute(select(m.OrgMember).where(m.OrgMember.id.in_(member_ids))))
            .scalars()
            .all()
        }
        if member_ids
        else {}
    )
    departments = (
        {
            r.id: r
            for r in (await db.execute(select(m.Department).where(m.Department.id.in_(dept_ids))))
            .scalars()
            .all()
        }
        if dept_ids
        else {}
    )
    return [_share_dto(s, members, departments) for s in shares]


@router.get("/boards/{board_id}/shares", response_model=list[BoardShareDTO])
async def list_board_shares(
    board_id: uuid.UUID, request: Request, db: DbSession, actor: BoardReader
) -> list[BoardShareDTO]:
    board = await _sharer(request, db, actor, board_id)
    shares = list(
        (
            await db.execute(
                select(m.DataBoardShare)
                .where(m.DataBoardShare.board_id == board.id)
                .order_by(m.DataBoardShare.created_at, m.DataBoardShare.id)
            )
        )
        .scalars()
        .all()
    )
    return await _share_dtos(db, shares)


@router.post(
    "/boards/{board_id}/shares",
    response_model=BoardShareDTO,
    status_code=status.HTTP_201_CREATED,
)
async def create_board_share(
    board_id: uuid.UUID,
    body: BoardShareRequest,
    request: Request,
    db: DbSession,
    actor: BoardReader,
) -> BoardShareDTO:
    board = await _sharer(request, db, actor, board_id)
    try:
        share = await share_board(
            db,
            board=board,
            actor_member_id=actor.member.id,
            member_id=body.member_id,
            department_id=body.department_id,
            principal=actor.principal,
        )
    except ShareTargetInvalid as exc:
        raise HTTPException(422, "board_share:invalid_target") from exc
    # Read back BEFORE committing: a commit unbinds the tenant for the rest of
    # the request (see `require_departmental`).
    (dto,) = await _share_dtos(db, [share])
    await db.commit()
    return dto


@router.delete("/boards/{board_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board_share(
    board_id: uuid.UUID,
    share_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: BoardReader,
) -> Response:
    board = await _sharer(request, db, actor, board_id)
    try:
        await unshare_board(
            db,
            board=board,
            share_id=share_id,
            actor_member_id=actor.member.id,
            principal=actor.principal,
        )
    except ShareNotFound as exc:
        raise HTTPException(404, "share not found") from exc
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/boards/{board_id}/share-targets", response_model=ShareTargetsDTO)
async def board_share_targets(
    board_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: BoardReader,
    q: Annotated[str, Query(max_length=200)] = "",
) -> ShareTargetsDTO:
    """Live members and departments of the tenant to share with. Its own route
    because `GET /members` needs `member:view`, which a sharer may lack; it
    answers names and ids only, and only to someone who may share this board."""
    await _sharer(request, db, actor, board_id)
    tenant_id = actor.principal.tenant_id
    typed = q.strip()
    pattern = f"%{_escape_like(typed)}%"
    member = m.OrgMember
    # Names by substring; an email only when the WHOLE address is typed, so a
    # sharer without member:view cannot enumerate addresses letter by letter.
    email_match = func.lower(member.email) == typed.lower() if "@" in typed else false()
    members = (
        (
            await db.execute(
                select(member)
                .where(
                    member.tenant_id == tenant_id,
                    member.deleted_at.is_(None),
                    or_(
                        member.display_name.ilike(pattern, escape="\\"),
                        email_match,
                    ),
                )
                .order_by(func.lower(member.display_name), member.id)
                .limit(_TARGET_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    dept = m.Department
    departments = (
        (
            await db.execute(
                select(dept)
                .where(
                    dept.tenant_id == tenant_id,
                    dept.deleted_at.is_(None),
                    dept.name.ilike(pattern, escape="\\"),
                )
                .order_by(func.lower(dept.name), dept.id)
                .limit(_TARGET_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return ShareTargetsDTO(
        members=[
            ShareTargetDTO(id=str(r.id), name=_member_name(r, typed_email=typed)) for r in members
        ],
        departments=[ShareTargetDTO(id=str(r.id), name=r.name) for r in departments],
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
