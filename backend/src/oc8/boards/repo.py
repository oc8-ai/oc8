"""The scoped repository for data boards: the one door a person's read goes through.

A board is visible exactly when its producing agent's department is: `agent:view`
resolved in the board's department (every seat carries it), or held tenant-wide.
`tenant_wide` is resolved by the caller, `tenant_wide_read(authority, perm(AGENT,
VIEW))`, as `api/v1/agents.py` and `api/v1/reports.py` do.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from oc8 import models as m
from oc8.authz.permissions import AGENT, MANAGE, perm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.authority import Authority
    from oc8.authz.scope import DepartmentScope


async def visible_boards(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
) -> list[m.DataBoard]:
    """Live boards (neither archived nor removed) the caller may see, by title.

    An empty scope that is not tenant-wide returns `[]` before any SQL is built:
    `department_id IN ()` is not valid SQL, and skipping the filter would fail open.
    """
    if not tenant_wide and not scope.viewable:
        return []
    stmt = select(m.DataBoard).where(
        m.DataBoard.tenant_id == tenant_id,
        m.DataBoard.deleted_at.is_(None),
        m.DataBoard.removed_at.is_(None),
    )
    if not tenant_wide:
        stmt = stmt.where(m.DataBoard.department_id.in_(scope.viewable))
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
        return row
    return None


def may_delete_board(authority: Authority, scope: DepartmentScope, board: m.DataBoard) -> bool:
    """Who may remove a board (spec §6.8.6): `agent:manage` tenant-wide, or in its department."""
    return (
        perm(AGENT, MANAGE) in authority.tenant_wide
        or board.department_id in scope.agent_manage_departments
    )
