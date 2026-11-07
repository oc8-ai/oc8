"""The only legal way to load or list an `Agent` for a human's departmental
READ (§0.B of the department-scoped-agent-authority design).

Modelled directly on `approvals/repo.py` -- see that module's docstring for why
a funnel exists at all rather than a discipline every future screen is expected
to remember on its own. `tests/agents/test_reads_go_through_the_scoped_
repository.py` is the sweep that makes walking around this file, for `Agent` or
`Department`, a decision somebody has to write a sentence for rather than a
typo nobody notices.

`tenant_wide` is a caller-resolved flag and deliberately NOT `scope.
is_unrestricted`: the latter is `approval:view_any`, an auditor's grant that has
nothing to do with reading agents. The caller (a route behind `require_
departmental(perm(AGENT, VIEW))`, once wired) resolves `tenant_wide` itself from
`perm(AGENT, VIEW) in authority.tenant_wide` -- the same RESOLVED-permission read
every other gate in this codebase uses, never `role_has(...)`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from oc8.models.core import Agent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.scope import DepartmentScope


async def visible_agents(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    department_id: uuid.UUID | None = None,
    search: str | None = None,
    status: str | None = None,
    group_by: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    include_archived: bool = False,
) -> tuple[list[Agent], int]:
    """Every agent this caller may see, plus a total count for the Design System
    Consistency plan's uniform search/filter/group/pagination contract (spec
    §1.1). Returns `(rows, total_count)`; a caller that omits `limit` gets
    every visible row, ordered as before (oldest first, or grouped), with
    `total_count == len(rows)` -- there is no page to count against.

    `department_id` INTERSECTS the scope and never widens it -- the department
    picker on the screen, not a second door. Asking for a department outside
    scope returns `([], 0)`, never a 403: a 403 there would confirm that
    department exists to a caller who has no business finding out.

    Empty scope and not tenant-wide returns `([], 0)` before any statement is
    built, same shape as `approvals/repo.py::visible_approvals` -- `WHERE
    department_id IN ()` is not valid SQL, so skipping the filter when the set
    is empty is the natural way to write this, and it fails OPEN (every agent
    in the tenant) if written that way. The early return is what keeps it
    failing CLOSED instead.

    The tenant Assistant is never listed. It is auto-provisioned, has no
    configurable anything, and is reached through its own door (`GET /assistant`,
    which looks it up by the flag and not through here) -- so on every screen a
    person browses agents on it is one more row they cannot act on and are
    invited to try. Filtered here rather than on each screen because this
    function is the single funnel every one of those screens goes through
    (`tests/agents/test_reads_go_through_the_scoped_repository.py`).
    """
    if not tenant_wide and not scope.viewable:
        return [], 0

    # Deferred import: oc8.api.v1._listquery lives under the api package, whose
    # __init__ aggregates oc8.api.v1.agents, which imports this module --
    # a top-level import here would be a circular import on any entry point
    # that reaches this module before oc8.api.v1 (e.g. the Copilot control
    # tools). Resolved once, at first call.
    from oc8.api.v1._listquery import apply_group_order, apply_search, paginate

    stmt = select(Agent).where(Agent.is_tenant_assistant.is_(False))
    if not include_archived:
        stmt = stmt.where(Agent.deleted_at.is_(None))
    if not tenant_wide:
        stmt = stmt.where(Agent.department_id.in_(scope.viewable))

    if department_id is not None:
        if not (tenant_wide or department_id in scope.viewable):
            return [], 0
        stmt = stmt.where(Agent.department_id == department_id)

    if status:
        stmt = stmt.where(Agent.status == status)

    stmt = apply_search(stmt, model=Agent, columns=[Agent.name, Agent.role_title], search=search)
    stmt = apply_group_order(
        stmt,
        model=Agent,
        group_by=group_by,
        group_fields={"departmentId": Agent.department_id, "status": Agent.status},
        default_order=Agent.created_at,
    )
    if limit is None:
        rows = list((await db.execute(stmt)).scalars().all())
        return rows, len(rows)
    return await paginate(db, stmt, limit=limit, offset=offset)


async def visible_agent(
    db: AsyncSession,
    *,
    scope: DepartmentScope,
    tenant_wide: bool,
    agent_id: uuid.UUID,
    include_tenant_assistant: bool = False,
) -> Agent | None:
    """One agent, or `None` for BOTH "no such row" and "exists outside scope".

    The caller 404s on either without telling them apart -- the same oracle
    guard `approvals/repo.py::load_for_actor` documents, applied here so a
    seat-holder who has plainly seen her own department cannot use a foreign
    agent id to learn whether it exists.

    An explicit `select()` rather than `db.get()`, and for the same reason that
    module gives: this load is a security boundary, and `Session.get` can return
    an already-loaded instance out of the identity map without issuing SQL at
    all -- the one thing a boundary check must never skip.

    `include_tenant_assistant` is off by default, matching `visible_agents`: the
    Assistant is not something a person manages, so every agent screen 404s on
    it. Exactly one caller passes True -- `chat_agent_for` (`api/v1/chat.py`,
    behind `POST /chat/sessions` and a member's own guardrail routes), which
    has ALREADY established through
    `_assistant_visible` that this caller holds `copilot:manage` and that this
    id is the tenant's Assistant. It is a parameter and not a second function
    so the sweep in `tests/agents/test_reads_go_through_the_scoped_repository.py`
    keeps covering the one door that can see it.
    """
    stmt = select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None))
    if not include_tenant_assistant:
        stmt = stmt.where(Agent.is_tenant_assistant.is_(False))
    row = (
        await db.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if row is None:
        return None
    if tenant_wide or row.department_id in scope.viewable:
        return row
    return None
