"""The scoped repository for data boards: the one door a person's read goes through.

A board is visible exactly when its producing agent's department is: `agent:view`
resolved in the board's department (every seat carries it), or held tenant-wide.
`tenant_wide` is resolved by the caller, `tenant_wide_read(authority, perm(AGENT,
VIEW))`, as `api/v1/agents.py` and `api/v1/reports.py` do.

A person (`member_id` given) also sees a board SHARED with them: a share naming
them, or naming a live department in which they hold a live seat -- and only
while the board's own department is live. Shares grant reading and querying
that one board and nothing else, so callers that ask "which boards may this
person change" (the builder) pass no `member_id`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, exists, false, not_, or_, select
from sqlalchemy.orm import aliased

from oc8 import models as m
from oc8.authz.permissions import AGENT, MANAGE, perm
from oc8.departments.repo import is_private_to, private_department_ids

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.authority import Authority
    from oc8.authz.scope import DepartmentScope


def _shared_with(member_id: uuid.UUID) -> ColumnElement[bool]:
    """`data_board` rows shared with the member, as a correlated predicate.

    A department share counts only through a seat that is live NOW and a
    department that is live: losing the seat, or archiving either department,
    takes the board away on the very next read, with nothing to clean up.
    """
    share = m.DataBoardShare
    seat = m.OrgMemberDepartment
    # Aliases, so neither department can auto-correlate to an outer query's.
    shared_dept = aliased(m.Department, name="shared_department")
    direct = exists().where(share.board_id == m.DataBoard.id, share.member_id == member_id)
    via_department = exists().where(
        share.board_id == m.DataBoard.id,
        share.department_id == shared_dept.id,
        shared_dept.deleted_at.is_(None),
        seat.department_id == share.department_id,
        seat.member_id == member_id,
        seat.revoked_at.is_(None),
    )
    board_dept = aliased(m.Department, name="board_department")
    board_department_live = exists().where(
        board_dept.id == m.DataBoard.department_id, board_dept.deleted_at.is_(None)
    )
    return and_(board_department_live, or_(direct, via_department))


def board_visibility(
    *, scope: DepartmentScope, tenant_wide: bool, member_id: uuid.UUID | None = None
) -> ColumnElement[bool]:
    """The visibility rule as one SQL predicate over `data_board`: what
    `visible_boards` filters on and what the notification inbox correlates to.

    Never `department_id IN ()`: an empty scope contributes `false()`, not a
    skipped filter (which would fail open).
    """
    # A personal agent's board is its owner's (spec A4): neither tenant-wide
    # `agent:view` nor a seat reaches it for anybody else. A share still
    # does -- the owner chose to give that one board to that person.
    not_private = not_(m.DataBoard.department_id.in_(private_department_ids(scope)))
    terms: list[ColumnElement[bool]] = []
    if tenant_wide:
        terms.append(not_private)
    elif scope.viewable:
        terms.append(and_(m.DataBoard.department_id.in_(scope.viewable), not_private))
    if member_id is not None:
        terms.append(_shared_with(member_id))
    return or_(*terms) if terms else false()


async def visible_boards(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    member_id: uuid.UUID | None = None,
) -> list[m.DataBoard]:
    """Live boards (neither archived nor removed) the caller may see, by title.

    An empty scope that is not tenant-wide and has no `member_id` (so no share
    can count) returns `[]` before any SQL is built.
    """
    if not tenant_wide and not scope.viewable and member_id is None:
        return []
    stmt = select(m.DataBoard).where(
        m.DataBoard.tenant_id == tenant_id,
        m.DataBoard.deleted_at.is_(None),
        m.DataBoard.removed_at.is_(None),
        board_visibility(scope=scope, tenant_wide=tenant_wide, member_id=member_id),
    )
    if agent_id is not None:
        stmt = stmt.where(m.DataBoard.agent_id == agent_id)
    stmt = stmt.order_by(m.DataBoard.title, m.DataBoard.id)
    return list((await db.execute(stmt.execution_options(populate_existing=True))).scalars().all())


async def visible_board(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    tenant_id: uuid.UUID,
    board_id: uuid.UUID,
    member_id: uuid.UUID | None = None,
) -> m.DataBoard | None:
    """One board, or `None` for BOTH "no such row" and "outside scope" (the oracle
    guard of `agents.repo.visible_agent`). Includes removed boards (the caller
    answers 410); excludes archived ones.

    An explicit `select` with `populate_existing`: the scope is resolved per
    request, and a seat revoked since the last one must take effect on this one.
    """
    stmt = select(m.DataBoard).where(
        m.DataBoard.id == board_id,
        m.DataBoard.tenant_id == tenant_id,
        m.DataBoard.deleted_at.is_(None),
    )
    row = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        return None
    if tenant_wide or row.department_id in scope.viewable:
        kind = await db.scalar(
            select(m.Department.kind).where(m.Department.id == row.department_id)
        )
        if not is_private_to(scope, kind=kind, department_id=row.department_id):
            return row
    if member_id is not None and await is_shared_with(db, board_id=row.id, member_id=member_id):
        return row
    return None


async def is_shared_with(db: AsyncSession, *, board_id: uuid.UUID, member_id: uuid.UUID) -> bool:
    """Whether the board reaches the member through a share alone (`_shared_with`)."""
    stmt = select(m.DataBoard.id).where(m.DataBoard.id == board_id, _shared_with(member_id))
    return (await db.execute(stmt)).first() is not None


async def has_direct_share(db: AsyncSession, member_id: uuid.UUID) -> bool:
    """Whether any board is shared with this member by name -- what admits a
    member with no seat at all to the board routes (`require_departmental(...,
    or_board_share=True)`); a department share needs a seat, which admits anyway."""
    stmt = select(m.DataBoardShare.id).where(m.DataBoardShare.member_id == member_id).limit(1)
    return (await db.execute(stmt)).first() is not None


def may_delete_board(authority: Authority, scope: DepartmentScope, board: m.DataBoard) -> bool:
    """Who may remove a board (spec §6.8.6): `agent:manage` tenant-wide, or in its department."""
    return (
        perm(AGENT, MANAGE) in authority.tenant_wide
        or board.department_id in scope.agent_manage_departments
    )


def board_notice_visible(
    *, scope: DepartmentScope, tenant_wide: bool, member_id: uuid.UUID
) -> ColumnElement[bool]:
    """A `member_notification` row may be listed: it is no `board.available`
    notice, or its board is one `visible_board(..., member_id=member_id)` still
    returns (live, removed or not). A predicate rather than a filter over a
    fetched page, so the inbox's total, unread count and cursor paging all
    count exactly the rows it lists."""
    notice = m.MemberNotification
    board = select(m.DataBoard.id).where(
        m.DataBoard.id == notice.subject_id,
        m.DataBoard.deleted_at.is_(None),
        board_visibility(scope=scope, tenant_wide=tenant_wide, member_id=member_id),
    )
    return or_(notice.kind != "board.available", board.exists())
