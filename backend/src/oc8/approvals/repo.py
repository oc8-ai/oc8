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
employee every approval in the tenant. An empty scope gets a constant-false
department term here, never an `IN` -- it sees its own member-owned cards
(`is_member_owned`) and nothing else.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, extract, false, func, not_, or_, select

from oc8.models.ops import ApprovalRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.authz.scope import DecisionActor

#: A list endpoint with no ceiling is a denial of service with a friendly name;
#: §5 fixes the default at 100 and the ceiling at 500.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

#: The approval an offer of direct access raises (copilot direct access §3).
DIRECT_ACCESS_ACTION = "direct_access"


def is_member_owned(approval: ApprovalRequest) -> bool:
    """A card only one person answers: a direct-access offer, or a direct write
    no department holds (copilot direct access §3). Its title and purpose are
    that person's, so it is nobody else's to see either -- whatever
    `department_id` it carries."""
    info = (approval.payload or {}).get("direct_access")
    return approval.action_type == DIRECT_ACCESS_ACTION or (
        isinstance(info, dict) and info.get("decider") == "member"
    )


def direct_access_owner(approval: ApprovalRequest) -> uuid.UUID | None:
    """The member who alone decides this approval: the conversation's member
    for a direct-access offer, and for a direct write no department holds.
    None for every other approval -- and for an owned one whose member cannot
    be read, which therefore nobody decides (`is_member_owned` still holds)."""
    if not is_member_owned(approval):
        return None
    info = (approval.payload or {}).get("direct_access")
    if not isinstance(info, dict):
        return None
    try:
        return uuid.UUID(str(info.get("member_id")))
    except ValueError:
        return None


def _owned_clause() -> ColumnElement[bool]:
    """`is_member_owned`, in SQL. `coalesce` because `->>` on a missing key is
    NULL, and `NOT (x OR NULL)` is NULL -- which would hide every ordinary
    approval from every queue."""
    return or_(
        ApprovalRequest.action_type == DIRECT_ACCESS_ACTION,
        func.coalesce(ApprovalRequest.payload["direct_access"]["decider"].astext, "") == "member",
    )


def owned_by(member_id: uuid.UUID) -> ColumnElement[bool]:
    return and_(
        _owned_clause(),
        ApprovalRequest.payload["direct_access"]["member_id"].astext == str(member_id),
    )


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

    # A member-owned card (`is_member_owned`) is its owner's alone, from any
    # seat or none, and nobody else's -- not the department it is filed in,
    # not the unrestricted. Every other approval follows the department term.
    mine = owned_by(actor.member.id)
    if department_id is not None:
        if not scope.may_view(department_id):
            return []
        departmental: ColumnElement[bool] | None = ApprovalRequest.department_id == department_id
        # The department picker narrows to one department; the person's own
        # cards belong to none.
        mine = and_(mine, ApprovalRequest.department_id == department_id)
    elif scope.is_unrestricted:
        # No department predicate at all. This is the ONLY way `department_id IS
        # NULL` -- the tenant-scope budget incident -- is ever visible to anybody,
        # and `viewable` is empty for an unrestricted caller by design, so a
        # filter built from it would show a CEO nothing.
        departmental = None
    elif scope.viewable:
        departmental = ApprovalRequest.department_id.in_(scope.viewable)
    else:
        # Seatless: nothing departmental -- `IN ()` is never built -- only own cards.
        departmental = false()
    others = not_(_owned_clause())
    stmt = stmt.where(or_(mine, others if departmental is None else and_(others, departmental)))

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
    if is_member_owned(row):
        return row if direct_access_owner(row) == actor.member.id else None
    if not actor.scope.may_view(row.department_id):
        return None
    return row


async def member_direct_access_cards(
    db: AsyncSession, *, member_id: uuid.UUID
) -> list[ApprovalRequest]:
    """A member's own direct-access cards, oldest first: the ones the member
    decides (`decider == "member"`, filed under their id) that are still pending
    or back a live grant. Never another member's, from any seat."""
    from oc8.models.direct_access import DirectAccessGrant

    live_grants = select(DirectAccessGrant.approval_id).where(
        DirectAccessGrant.member_id == member_id,
        DirectAccessGrant.revoked_at.is_(None),
    )
    decided_by_member = (
        func.coalesce(ApprovalRequest.payload["direct_access"]["decider"].astext, "") == "member"
    )
    stmt = (
        select(ApprovalRequest)
        .where(
            owned_by(member_id),
            decided_by_member,
            or_(ApprovalRequest.status == "pending", ApprovalRequest.id.in_(live_grants)),
        )
        .order_by(ApprovalRequest.created_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def load_direct_access_card(
    db: AsyncSession, approval_id: uuid.UUID, *, member_id: uuid.UUID
) -> ApprovalRequest | None:
    """One direct-access card, or None for both "no such row" and "not this
    member's". An explicit select, never `db.get` (identity-map answer)."""
    row = (
        await db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or direct_access_owner(row) != member_id:
        return None
    return row


async def approved_awaiting_result(
    db: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID
) -> list[ApprovalRequest]:
    """A task's approved approvals that have no result link yet.

    No actor: the agent runtime reads its own task's approvals right after the
    approved call ran, to note the record it produced (spec §5 item 5). No
    human sees anything through this.
    """
    stmt = select(ApprovalRequest).where(
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.task_id == task_id,
        ApprovalRequest.status == "approved",
        ApprovalRequest.result_url.is_(None),
    )
    return list((await db.execute(stmt)).scalars().all())
