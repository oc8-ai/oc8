"""The only legal way to load or list a `Department` for a human's departmental
READ (§0.B of the department-scoped-agent-authority design).

Mirrors `agents/repo.py` exactly -- same three-branch shape, same None-on-
either-reason 404 contract, same reason `tenant_wide` is a caller-resolved flag
and not `scope.is_unrestricted`. See that module's docstring; not restated here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from oc8.models.core import Department

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.scope import DepartmentScope


async def visible_departments(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    search: str | None = None,
    group_by: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    include_archived: bool = False,
) -> tuple[list[Department], int]:
    """Every department this caller may see, plus a total count for the Design
    System Consistency plan's uniform search/filter/group/pagination contract
    (spec §1.1). Returns `(rows, total_count)`; a caller that omits `limit`
    gets every visible row, ordered as before (oldest first), with
    `total_count == len(rows)` -- there is no page to count against. Same
    shape as `agents.repo.visible_agents`; see that module's docstring for the
    reasoning this one does not restate.

    `group_by` is accepted for contract-uniformity with `visible_agents` even
    though `group_fields` is empty -- Department has no obvious groupable
    field beyond name/id, so a caller that passes one gets `ValueError`. Don't
    expose a group-by dropdown for Departments in the frontend config.

    The tenant Assistant's own department is never listed, for the same reason
    `visible_agents` never lists the Assistant itself: it is auto-provisioned to
    give that agent somewhere to stand, holds nothing anybody configures, and on
    the Office floor it reads as a department somebody forgot to fill in.
    """
    if not tenant_wide and not scope.viewable:
        return [], 0

    # Deferred import: oc8.api.v1._listquery lives under the api package, whose
    # __init__ aggregates oc8.api.v1.departments, which imports this module --
    # a top-level import here would be a circular import on any entry point
    # that reaches this module before oc8.api.v1 (e.g. the Copilot control
    # tools). Resolved once, at first call.
    from oc8.api.v1._listquery import apply_group_order, apply_search, paginate

    stmt = select(Department).where(Department.is_assistant_department.is_(False))
    if not include_archived:
        stmt = stmt.where(Department.deleted_at.is_(None))
    if not tenant_wide:
        stmt = stmt.where(Department.id.in_(scope.viewable))

    stmt = apply_search(stmt, model=Department, columns=[Department.name], search=search)
    stmt = apply_group_order(
        stmt,
        model=Department,
        group_by=group_by,
        group_fields={},
        default_order=Department.created_at,
    )
    if limit is None:
        rows = list((await db.execute(stmt)).scalars().all())
        return rows, len(rows)
    return await paginate(db, stmt, limit=limit, offset=offset)


async def visible_department(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    department_id: uuid.UUID,
) -> Department | None:
    """One department, or `None` for BOTH "no such row" and "exists outside
    scope" -- see `agents.repo.visible_agent` for the full reasoning; the same
    oracle guard applies here.

    The Assistant's department 404s like anything else nobody may see. Unlike
    `visible_agent` there is no opt-in: nothing in the Assistant's own machinery
    loads its department through this funnel (`get_or_create_assistant` holds
    the row it just made, and `assistant_visible` in `oc8/chat/reach.py` asks about the agent), so a
    bypass here would have no caller."""
    row = (
        await db.execute(
            select(Department)
            .where(
                Department.id == department_id,
                Department.deleted_at.is_(None),
                Department.is_assistant_department.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if tenant_wide or row.id in scope.viewable:
        return row
    return None
