"""The only legal way to load or list an approval for a HUMAN (§0.B, §4).

Not a convenience layer. `api/v1/feed.py` has listed approvals filtered on status
alone since the day it was written, and that is not because anybody decided the
whole company should see them -- it is because nothing forced a new list to say
whose they are. So the term lives here, `decide_approval` asks it again, and
`tests/approvals/test_reads_go_through_the_scoped_repository.py` fails the build
for a `select(m.ApprovalRequest)` written anywhere else.

The one shape this module exists to make unwritable:

    if scope.viewable:
        stmt = stmt.where(ApprovalRequest.department_id.in_(scope.viewable))

`WHERE department_id IN ()` is not valid SQL, so skipping the filter when the set
is empty is the natural way to write it -- and it fails OPEN, handing a seatless
employee every approval in the tenant. An empty scope returns an empty list here,
before any statement is built.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, extract, func, or_, select

from oc8.models.ops import ApprovalRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.scope import DecisionActor

#: A list endpoint with no ceiling is a denial of service with a friendly name;
#: §5 fixes the default at 100 and the ceiling at 500.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


async def visible_approvals(
    db: AsyncSession,
    *,
    actor: DecisionActor,
    status: str,
    department_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[ApprovalRequest]:
    """The approvals this actor may see, newest first.

    `actor` is keyword-only with no default for the same reason it is on
    `decide_approval`: a caller that could omit it is a caller that lists the
    whole tenant, and nothing would fail.

    `department_id` INTERSECTS the scope and never widens it. It is a filter over
    what you may already see -- the department picker an admin gets on the screen
    -- so asking for somebody else's department returns nothing rather than 403,
    because a 403 there would answer "does that department exist".
    """
    scope = actor.scope
    stmt = select(ApprovalRequest).where(ApprovalRequest.status == status)

    # A row routed to one member -- their own guardrail asked them (D3a) -- is
    # theirs alone: not the department's queue, and not an unrestricted
    # caller's either. Everything else keeps the department term below.
    unassigned = ApprovalRequest.assignee_member_id.is_(None)
    own = ApprovalRequest.assignee_member_id == actor.member.id
    if scope.is_unrestricted:
        # No department predicate at all. This is the ONLY way `department_id IS
        # NULL` -- the tenant-scope budget incident -- is ever visible to anybody,
        # and `viewable` is empty for an unrestricted caller by design, so a
        # filter built from it would show a CEO nothing.
        stmt = stmt.where(or_(unassigned, own))
    elif scope.viewable:
        stmt = stmt.where(
            or_(and_(unassigned, ApprovalRequest.department_id.in_(scope.viewable)), own)
        )
    else:
        # No seat anywhere still leaves the rows routed to this member. The
        # empty-`IN ()` hazard the module docstring names does not arise: the
        # predicate is an equality on the member, never the empty set.
        stmt = stmt.where(own)

    if department_id is not None:
        # The department picker filters the department QUEUE; a member's own
        # rows are not in it.
        if not scope.may_view(department_id):
            return []
        stmt = stmt.where(ApprovalRequest.department_id == department_id, unassigned)

    # `id` breaks the tie. `created_at` defaults to `now()`, which in Postgres is
    # TRANSACTION start time, so several approvals raised inside one request --
    # a held tool call and the budget incident that fired on the same step --
    # share it to the microsecond, and their relative order is then whatever the
    # plan happens to emit. Ids are uuid7 and time-ordered, so this is the same
    # "newest" measured by a finer clock, and it is stable between two requests.
    stmt = stmt.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc()).limit(
        max(1, min(limit, MAX_LIMIT))
    )
    return list((await db.execute(stmt)).scalars().all())


async def avg_wait_ms(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
) -> int | None:
    """Average time a DECIDED approval sat waiting for a human, in milliseconds.

    Not actor-scoped like `visible_approvals`/`load_for_actor` above: this is a
    single SQL-side `AVG(decided_at - created_at)` over `tenant_id`/`agent_id`/
    `department_id`/a date range -- no individual `ApprovalRequest` row, nor any
    field off one, ever reaches a caller. `kpis.aggregate.compute_kpis` is the
    one caller (its `statistics:view`-gated `GET /kpis` is deliberately
    tenant-wide, api/v1/kpis.py's own docstring), and this function lives here
    rather than beside it because this module is the one
    `tests/approvals/test_reads_go_through_the_scoped_repository.py` allows to
    load `ApprovalRequest` at all -- see that file's own docstring on why.

    Scoped by `ApprovalRequest.department_id` DIRECTLY, never by a join through
    `Agent.department_id` (spec §1.3): that column is denormalised at raise time
    precisely so moving an agent between departments does not drag its
    approvals into a queue nobody there was ever asked about.

    Undecided requests are excluded, so a scope where nothing has been decided
    returns `None` rather than `0`.
    """
    conditions: list[ColumnElement[bool]] = [
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.decided_at.isnot(None),
    ]
    if agent_id is not None:
        conditions.append(ApprovalRequest.agent_id == agent_id)
    if department_id is not None:
        conditions.append(ApprovalRequest.department_id == department_id)
    if date_from is not None:
        conditions.append(ApprovalRequest.created_at >= date_from)
    if date_to is not None:
        conditions.append(ApprovalRequest.created_at <= date_to)
    wait_ms = extract("epoch", ApprovalRequest.decided_at - ApprovalRequest.created_at) * 1000
    stmt = select(func.avg(wait_ms)).where(*conditions)
    value = (await db.execute(stmt)).scalar_one()
    if value is None:
        return None
    return round(float(value))


async def load_for_actor(
    db: AsyncSession, approval_id: uuid.UUID, *, actor: DecisionActor
) -> ApprovalRequest | None:
    """One approval, or `None` for BOTH "no such row" and "not yours".

    The caller 404s on either without telling them apart. That is the oracle
    guard: `AlreadyDecided`'s 409 carries the status and the time it was decided,
    and the realtime socket still announces `approval.created` tenant-wide
    (§10 item 5), so a caller holding ids off it could otherwise ask this
    endpoint which of them exist and when they were answered. The scope is
    therefore resolved BEFORE the row's state is examined.

    An explicit `select()` and never `db.get`: `Session.get` returns an
    already-loaded instance out of the identity map without issuing SQL, and this
    load is a security boundary -- one whose answer must come from the database
    and from this transaction's RLS binding, not from whatever the session
    happens to have seen earlier in the request.
    """
    row = (
        await db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.assignee_member_id is not None:
        # Routed to one member (D3a): visible to them and to nobody else,
        # whatever seat or `view_any` the caller holds.
        return row if row.assignee_member_id == actor.member.id else None
    if not actor.scope.may_view(row.department_id):
        return None
    return row


async def next_stage_statuses(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Member approval id -> status of the department stage that follows it (D3a).

    `ids` are member-routed rows the caller was already admitted to (through
    `visible_approvals` / `load_for_actor`, which show a member their own rows).
    Only the follow-up's STATUS comes back, keyed by the member's row: a member
    without a seat learns how the department decided their call, and nothing
    else of the department's row. One batched query on the tenant-bound session.
    """
    if not ids:
        return {}
    found = (
        await db.execute(
            select(ApprovalRequest.previous_approval_id, ApprovalRequest.status).where(
                ApprovalRequest.previous_approval_id.in_(ids)
            )
        )
    ).all()
    return {row[0]: row[1] for row in found if row[0] is not None}
