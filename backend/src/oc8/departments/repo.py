"""The only legal way to load or list a `Department` for a human's departmental
READ (§0.B of the department-scoped-agent-authority design).

Mirrors `agents/repo.py` exactly -- same three-branch shape, same None-on-
either-reason 404 contract, same reason `tenant_wide` is a caller-resolved flag
and not `scope.is_unrestricted`. See that module's docstring; not restated here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Select, select

from oc8.models.core import (
    DEPARTMENT_KIND_PERSONAL,
    DEPARTMENT_KIND_STANDARD,
    Department,
)
from oc8.models.identity import OrgMember, OrgMemberDepartment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.scope import DepartmentScope


def personal_reachable(
    *, owner_member_id: uuid.UUID | None, member_id: uuid.UUID | None, manage_all: bool
) -> bool:
    """Whether a caller reaches a personal department, or an agent of one, by
    id: its owner (`member_id`), or a holder of tenant-wide
    `department:manage` (`manage_all`, metadata only). The one allowlist
    `visible_department` and `agents.repo.visible_agent` share."""
    mine = member_id is not None and owner_member_id == member_id
    return mine or manage_all


def is_private_to(
    scope: DepartmentScope, *, kind: str | None, department_id: uuid.UUID | None
) -> bool:
    """Whether what an agent of this department did is private to somebody
    other than this caller (spec A4, ruling R1): its runs, run files, memory,
    activity details and approval payloads. True for a `personal` department
    the caller does not own (`scope.personal_owned`); always False for any
    other kind.

    The ONE predicate every content reader asks -- no tenant-wide grant, no
    every-agent reach and no `decide_any` overrides it, because ownership is
    `department.owner_member_id` and no role can make anybody the owner. It
    fails closed: a department whose owner has left is owned by nobody, so it
    is private to everybody. `private_department_ids` is the same rule as a
    query, for readers that narrow a statement."""
    return kind == DEPARTMENT_KIND_PERSONAL and department_id not in scope.personal_owned


def private_department_ids(scope: DepartmentScope) -> Select[tuple[uuid.UUID]]:
    """`is_private_to`, as a statement: the ids of every personal department
    this caller does not own -- all of them when `scope.personal_owned` is
    empty. Soft-deleted departments included: their agents' history stays
    private after the department goes. Tenant-scoped by RLS."""
    stmt = select(Department.id).where(Department.kind == DEPARTMENT_KIND_PERSONAL)
    if scope.personal_owned:
        stmt = stmt.where(Department.id.not_in(scope.personal_owned))
    return stmt


def personal_content_hidden(
    *, kind: str | None, department_id: uuid.UUID | None, scope: DepartmentScope
) -> bool:
    """Whether a caller who reached an agent sees its metadata only (spec
    decision 3): `is_private_to` under its PR 1 name, kept so every call site
    asks the one rule. A `department:manage` holder who got past
    `personal_reachable` sees name, role, tools and status, never the
    mission, instructions or anything the agent did."""
    return is_private_to(scope, kind=kind, department_id=department_id)


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

    A `personal` department is never listed either, not even to its owner or a
    tenant-wide reader: it is reached by id, and its agents through `visible_agents`.
    """
    if not tenant_wide and not scope.viewable:
        return [], 0

    # Deferred import: oc8.api.v1._listquery lives under the api package, whose
    # __init__ aggregates oc8.api.v1.departments, which imports this module --
    # a top-level import here would be a circular import on any entry point
    # that reaches this module before oc8.api.v1 (e.g. the Copilot control
    # tools). Resolved once, at first call.
    from oc8.api.v1._listquery import apply_group_order, apply_search, paginate

    stmt = select(Department).where(Department.kind == DEPARTMENT_KIND_STANDARD)
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


async def live_department(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    for_update: bool = False,
) -> Department | None:
    """One live (not soft-deleted) department by id, any `kind`, WITHOUT the
    caller's scope. Only for paths that have already decided the caller may act
    on this department -- e.g. `capas.service.instantiate_agent`, reached from
    the admin route behind tenant-wide `plugin:manage`, or from the member's
    hire with the department the member owns. Never for a human's browse:
    that is `visible_department`.

    `for_update` takes a row lock (`SELECT ... FOR UPDATE`) held until the
    transaction ends, so a check-then-write on the department -- the personal
    agent cap, a held frame write -- serialises with a concurrent one. The
    locked row is read afresh, also into an instance already loaded."""
    stmt = select(Department).where(
        Department.id == department_id,
        Department.tenant_id == tenant_id,
        Department.deleted_at.is_(None),
    )
    if for_update:
        # Re-read the row as it is once locked: a department this session
        # loaded earlier must not keep the values from before the lock.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def department_kind(
    db: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> str | None:
    """The `kind` of one department by id, archived ones included, or None for
    no such department in this tenant. Without the caller's scope, and only
    the kind: for the agent-write door (`api.deps.authorize_agent_write`),
    which must tell a personal department apart before it decides who may
    write an agent there."""
    stmt = select(Department.kind).where(
        Department.id == department_id, Department.tenant_id == tenant_id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def lock_departments_owned_by(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> list[uuid.UUID]:
    """Row-lock (`SELECT ... FOR UPDATE`) every department a member owns, any
    `kind` and archived ones too, and return their ids. Without the caller's
    scope: only for the offboarding sweep
    (`departments.personal.stop_departments_owned_by`), which must serialise
    with a concurrent hire (`ensure_personal_capacity` takes the same lock)
    before it reads the departments' agents. Ordered by id, so two sweeps lock
    in the same order. Held until the transaction ends."""
    stmt = (
        select(Department.id)
        .where(Department.tenant_id == tenant_id, Department.owner_member_id == member_id)
        .order_by(Department.id)
        .with_for_update()
    )
    return list((await db.execute(stmt)).scalars().all())


async def visible_department(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    department_id: uuid.UUID,
    member_id: uuid.UUID | None = None,
    manage_all: bool = False,
) -> Department | None:
    """One department, or `None` for BOTH "no such row" and "exists outside
    scope" -- see `agents.repo.visible_agent` for the full reasoning; the same
    oracle guard applies here.

    The Assistant's department 404s like anything else nobody may see. Unlike
    `visible_agent` there is no opt-in: nothing in the Assistant's own machinery
    loads its department through this funnel (`get_or_create_assistant` holds
    the row it just made, and `_assistant_visible` asks about the agent), so a
    bypass here would have no caller.

    An allowlist on `kind`: a `standard` department follows the scope; a
    `personal` one is returned only to its owner (`member_id`) or to a caller
    holding tenant-wide `department:manage` (`manage_all`). Tenant-wide READ
    alone never exposes it. Both default to "no", so a caller that passes
    neither cannot see a personal department."""
    row = (
        await db.execute(
            select(Department)
            .where(
                Department.id == department_id,
                Department.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.kind == DEPARTMENT_KIND_PERSONAL:
        reachable = personal_reachable(
            owner_member_id=row.owner_member_id, member_id=member_id, manage_all=manage_all
        )
        return row if reachable else None
    if row.kind != DEPARTMENT_KIND_STANDARD:
        return None
    if tenant_wide or row.id in scope.viewable:
        return row
    return None


async def member_departments(db: AsyncSession, *, member: OrgMember) -> list[Department]:
    """The departments one member BELONGS to -- a live seat, or every department
    for `all_departments` -- ordered by name. Never the Assistant's own, never an
    archived one, and never another department of the tenant: a member with no
    seat gets nothing (fail closed). Never a `personal` department, not even the
    member's own: belonging to a team is what this answers, and that is not one.

    `populate_existing`: a frame edited since this session loaded the row (it
    keeps rows across commits) must be seen on the very next call."""
    stmt = (
        select(Department)
        .execution_options(populate_existing=True)
        .where(
            Department.tenant_id == member.tenant_id,
            Department.deleted_at.is_(None),
            Department.kind == DEPARTMENT_KIND_STANDARD,
        )
        .order_by(Department.name, Department.id)
    )
    if not member.all_departments:
        seated = select(OrgMemberDepartment.department_id).where(
            OrgMemberDepartment.member_id == member.id,
            OrgMemberDepartment.revoked_at.is_(None),
        )
        stmt = stmt.where(Department.id.in_(seated))
    return list((await db.execute(stmt)).scalars().all())
