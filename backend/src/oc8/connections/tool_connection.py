"""The one connection row of a tool (unified connections design, "Storage").

A tool has one `McpConnection` per tenant: the bridge definition, in no
department and pinned to no login. Which login it runs with is decided by
`connections.login.resolve_login`, and whether an agent or a person reaches
the tool at all by the frame policy (`authz.pdp.reachable_connections`) --
never by the department a row happens to carry.

Until a tenant's per-department setups are consolidated, a department still
owns its own row of the tool (with the login values copied into it): that
row keeps serving the department, so nothing stops working before the
consolidation ran. A department-less row that is no integration (a custom
server registered before it was given a department) is returned too -- the
Copilot reaches it through a department policy -- but an agent's run does not
until it is bound to a department (`usable.needs_department`); callers on an
agent's path check that.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m


def _department_list(
    department_id: uuid.UUID | Sequence[uuid.UUID] | None,
) -> list[uuid.UUID]:
    if department_id is None:
        return []
    if isinstance(department_id, uuid.UUID):
        return [department_id]
    return list(department_id)


async def _pick(
    db: AsyncSession,
    *,
    names: Sequence[str],
    department_ids: Sequence[uuid.UUID],
    connected_only: bool,
) -> dict[str, m.McpConnection]:
    if not names:
        return {}
    where = [
        m.McpConnection.name.in_(list(names)),
        m.McpConnection.credential_id.is_(None),
    ]
    if connected_only:
        where.append(m.McpConnection.connected.is_(True))
    placed: ColumnElement[bool] = m.McpConnection.department_id.is_(None)
    if department_ids:
        placed = placed | m.McpConnection.department_id.in_(list(department_ids))
    rows = (
        (
            await db.execute(
                select(m.McpConnection)
                .execution_options(populate_existing=True)
                .where(*where, placed)
                .order_by(m.McpConnection.created_at, m.McpConnection.id)
            )
        )
        .scalars()
        .all()
    )
    own: dict[str, m.McpConnection] = {}
    shared: dict[str, m.McpConnection] = {}
    for row in rows:
        if row.department_id is not None:
            # Unconsolidated: the department's own row, oldest first.
            own.setdefault(row.name, row)
        else:
            shared.setdefault(row.name, row)
    return {name: own.get(name) or shared[name] for name in names if name in own or name in shared}


async def tool_connection(
    db: AsyncSession,
    *,
    name: str,
    department_id: uuid.UUID | Sequence[uuid.UUID] | None,
    connected_only: bool = True,
) -> m.McpConnection | None:
    """The tool's connection row: the tenant's department-less row of `name`;
    while the tenant is unconsolidated, the row `department_id` (or the oldest
    of several departments') still owns. Rows pinned to one login are never
    the tool row. `connected_only=False` also returns a row nobody tested yet
    (an integration's materialised row starts unconnected)."""
    found = await _pick(
        db,
        names=[name],
        department_ids=_department_list(department_id),
        connected_only=connected_only,
    )
    return found.get(name)


async def tool_connections(
    db: AsyncSession,
    *,
    names: Iterable[str],
    department_id: uuid.UUID | Sequence[uuid.UUID] | None,
    connected_only: bool = True,
) -> list[m.McpConnection]:
    """`tool_connection` for each of `names` that has one, in `names` order
    (duplicates once)."""
    wanted = list(dict.fromkeys(names))
    found = await _pick(
        db,
        names=wanted,
        department_ids=_department_list(department_id),
        connected_only=connected_only,
    )
    return [found[name] for name in wanted if name in found]


async def tool_names(db: AsyncSession, *, department_id: uuid.UUID | None) -> list[str]:
    """Every tool name a row exists for that `tool_connection` could return
    for `department_id` (the department's own rows and the shared ones),
    sorted."""
    placed: ColumnElement[bool] = m.McpConnection.department_id.is_(None)
    if department_id is not None:
        placed = placed | (m.McpConnection.department_id == department_id)
    names = (
        await db.execute(
            select(m.McpConnection.name)
            .where(placed, m.McpConnection.credential_id.is_(None))
            .distinct()
        )
    ).scalars()
    return sorted(set(names))


async def unconsolidated_connection(
    db: AsyncSession, *, department_id: uuid.UUID | None
) -> m.McpConnection | None:
    """The oldest connected row a department still owns (no login pin): the
    run's primary connection for a department set up before consolidation,
    or a custom server bound to it. None once its tools are shared rows --
    those reach a run through the policy (`runtime_env.integration_connections`)."""
    if department_id is None:
        return None
    row: m.McpConnection | None = await db.scalar(
        select(m.McpConnection)
        .where(
            m.McpConnection.department_id == department_id,
            m.McpConnection.connected.is_(True),
            m.McpConnection.credential_id.is_(None),
        )
        .order_by(m.McpConnection.created_at, m.McpConnection.id)
        .limit(1)
    )
    return row
