"""People and seats: the writes behind `/members`, and the reads behind `/me` (§1, §5).

Everything an administrator does to somebody's authority happens here, in one
place, because there are exactly two ways to widen it -- `all_departments` and a
seat -- and both of them release money.

**Nothing in this module commits.** Services flush; the caller commits. A commit
inside `tenant_session` unbinds `app.tenant_id` for every statement after it, and
in a route body that is silent rather than loud: the next read simply returns
nothing.

**Revoked, not deleted.** A seat that is taken away keeps its row and gains a
`revoked_at`. "Who could approve this, and until when" is the first question an
audit asks, and a DELETE answers it with silence. It is also what the partial
unique index is partial ON, so the history costs nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oc8.api.v1._listquery import apply_group_order, apply_search, paginate
from oc8.authz.permissions import SEAT_PERMISSIONS
from oc8.db.base import uuid7
from oc8.models.core import Department, Role
from oc8.models.identity import OrgMember, OrgMemberDepartment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

#: A tenant's people list is a screen, not an export. Same ceiling as the other
#: two list doors in this slice, for the same reason.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


class UnknownSeatRole(ValueError):
    """A seat role outside the closed vocabulary.

    Raised rather than defaulted. `seat_permissions_for` fail-closes to the empty
    set, so a typo would otherwise write a row the CHECK constraint happens to
    reject -- or, if it did not, a seat that carries nothing and looks granted on
    every screen.
    """


@dataclass(frozen=True)
class Seat:
    """One live seat, with the department's name already resolved."""

    department_id: uuid.UUID
    department_name: str
    seat_role: str
    #: WRITE authority for Agent, in exactly this department -- orthogonal to
    #: `seat_role`, never a permission string. See `OrgMemberDepartment.
    #: agent_manage` and `authz/scope.py::DepartmentScope._agent_manage`.
    agent_manage: bool = False


@dataclass(frozen=True)
class MemberRow:
    """A person, their live seats, and the role somebody assigned them.

    The role is here because until it was, nothing in the product could answer
    *"what does Anna hold"*. The only view of an assignment was a ROLE's holder
    list, so the question cost one panel expansion per role -- and the assignment
    picker offered a person whose current role it could not show and then replaced
    it silently, recording the previous role in an audit event no screen renders.
    """

    id: uuid.UUID
    subject: str
    display_name: str
    all_departments: bool
    first_seen_at: dt.datetime
    seats: tuple[Seat, ...]
    #: The assignment, or `None` for the token floor -- which is not "no
    #: permissions" and must not be rendered as one.
    role_id: uuid.UUID | None = None
    #: Its name, resolved. Empty when there is no assignment, and also when the
    #: assignment points at a row that is gone: a uuid on a screen is not a name,
    #: and the resolver already treats that row as granting nothing.
    role_name: str = ""


def validated_seat_role(seat_role: str) -> str:
    if seat_role not in SEAT_PERMISSIONS:
        raise UnknownSeatRole(
            f"unknown seat role {seat_role!r}; expected one of "
            f"{', '.join(sorted(SEAT_PERMISSIONS))}"
        )
    return seat_role


async def seats_for_many(
    db: AsyncSession,
    member_ids: Sequence[uuid.UUID],
    *,
    include_archived: bool = True,
) -> dict[uuid.UUID, list[Seat]]:
    """Every live seat for these people, in one query.

    One query and not one per person: the members screen lists a tenant, and the
    obvious shape -- resolve each person's seats as you render them -- is how a
    company of two hundred becomes two hundred and one round trips.

    `include_archived` is which QUESTION is being asked, and the two callers ask
    different ones:

    * **True** -- "what is on record". `GET /members` must show a live seat in an
      an archived department, because that is the screen that has to revoke it,
      and a row nobody can see is a row nobody takes away.
    * **False** -- "where does this person stand". `GET /me` and
      `GET /governance` answer authority, and `authz/scope.py` stopped counting a
      seat in an archived department when it started joining `Department`. A
      seat listed there that grants nothing would put the workspace in the wrong
      empty state: `seats` non-empty means "nothing is waiting for you" rather
      than "nobody has assigned you to a department", which is the one
      distinction §7 says matters more than anything else on the page.
    """
    if not member_ids:
        return {}
    stmt = (
        select(
            OrgMemberDepartment.member_id,
            OrgMemberDepartment.department_id,
            OrgMemberDepartment.seat_role,
            OrgMemberDepartment.agent_manage,
            Department.name,
        )
        # LEFT, so a renamed-away department does not make a live seat disappear
        # from the screen that has to revoke it. The same choice
        # `workspace/queue.py` makes, and for the same reason.
        .outerjoin(Department, Department.id == OrgMemberDepartment.department_id)
        .where(
            OrgMemberDepartment.member_id.in_(list(member_ids)),
            # The table keeps revoked rows so an audit can answer "and until
            # when". Reading every row for the member -- the obvious query --
            # shows a revoked approver as still seated.
            OrgMemberDepartment.revoked_at.is_(None),
        )
        .order_by(Department.name, OrgMemberDepartment.department_id)
    )
    if not include_archived:
        # `Department.id.is_not(None)` too: the outer join leaves NULLs for a seat
        # whose department row does not exist at all, and `deleted_at IS NULL` is
        # true of a row that is not there. `authz/scope.py` refuses that seat with
        # an inner join, and these two must agree about who stands where.
        stmt = stmt.where(Department.id.is_not(None), Department.deleted_at.is_(None))
    rows = (await db.execute(stmt)).all()

    out: dict[uuid.UUID, list[Seat]] = {}
    for member_id, department_id, seat_role, agent_manage, department_name in rows:
        out.setdefault(member_id, []).append(
            Seat(
                department_id=department_id,
                department_name=department_name or "",
                seat_role=seat_role,
                agent_manage=agent_manage,
            )
        )
    return out


async def seats_for(
    db: AsyncSession, member_id: uuid.UUID, *, include_archived: bool = True
) -> list[Seat]:
    return (await seats_for_many(db, [member_id], include_archived=include_archived)).get(
        member_id, []
    )


async def list_members(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    search: str | None = None,
    group_by: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[MemberRow], int]:
    """Everybody this tenant knows, oldest first, with their live seats.

    Ordered by `created_at, id` rather than by name: two people enrolled in the
    same transaction share `created_at` to the microsecond (Postgres `now()` is
    transaction start time), and a tie under LIMIT is a list that can repeat or
    skip a row between two requests.

    Returns `(rows, total_count)` for the Design System Consistency plan's
    uniform search/filter/group/pagination contract (spec §1.1) -- same shape
    as `agents.repo.visible_agents` and `departments.repo.visible_departments`.
    `search` matches against `display_name`. `group_by` is accepted for
    contract-uniformity with those two callers even though `group_fields` is
    empty: the only other candidate, the assigned role's name, is resolved
    from a second query in `role_names_for` (see `MemberRow.role_name`'s own
    docstring) rather than being a real column on `OrgMember`, so it cannot be
    named in an `ORDER BY` without a join this function does not do -- same
    precedent as `visible_departments`. A caller that passes a `group_by`
    anyway gets `ValueError`.
    """
    stmt = select(OrgMember).where(OrgMember.tenant_id == tenant_id, OrgMember.deleted_at.is_(None))
    stmt = apply_search(stmt, model=OrgMember, columns=[OrgMember.display_name], search=search)
    stmt = apply_group_order(
        stmt,
        model=OrgMember,
        group_by=group_by,
        group_fields={},
        default_order=OrgMember.created_at,
    )
    # `apply_group_order` orders by `default_order` alone, which is not unique
    # on its own -- see the docstring above on why a tie under LIMIT is a list
    # that can repeat or skip a row between two requests. Same tiebreaker the
    # unpaginated version always used.
    stmt = stmt.order_by(OrgMember.id)
    people, total = await paginate(db, stmt, limit=max(1, min(limit, MAX_LIMIT)), offset=offset)

    by_member = await seats_for_many(db, [p.id for p in people])
    names = await role_names_for(db, tenant_id=tenant_id, role_ids=[p.role_id for p in people])
    rows = [
        MemberRow(
            id=p.id,
            subject=p.subject,
            display_name=p.display_name,
            all_departments=p.all_departments,
            first_seen_at=p.created_at,
            seats=tuple(by_member.get(p.id, [])),
            role_id=p.role_id,
            role_name="" if p.role_id is None else names.get(p.role_id, ""),
        )
        for p in people
    ]
    return rows, total


async def role_names_for(
    db: AsyncSession, *, tenant_id: uuid.UUID, role_ids: Sequence[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    """Name every one of these roles in ONE query.

    Not one per person: the picker on the five-hundred-person tenant lists five
    hundred people holding perhaps six roles between them, and resolving the name
    as each row is rendered is how a screen nobody thinks of as expensive becomes
    five hundred round trips.

    Soft-deleted rows are read, deliberately. An assignment pointing at a deleted
    role grants nothing, and the screen has to be able to SAY which role that was
    -- "holds a role that no longer exists" with no name is a state an
    administrator cannot repair.
    """
    wanted = {role_id for role_id in role_ids if role_id is not None}
    if not wanted:
        return {}
    rows = (
        await db.execute(
            select(Role.id, Role.name).where(Role.tenant_id == tenant_id, Role.id.in_(wanted))
        )
    ).all()
    return {role_id: name for role_id, name in rows}


async def get_member(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> OrgMember | None:
    return (
        (
            await db.execute(
                select(OrgMember).where(
                    # The tenant predicate is written out although RLS already
                    # applies it: this row is about to be handed authority, and a
                    # read that would be tenant-wide on an unbound session is not
                    # a read to leave implicit.
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.id == member_id,
                    OrgMember.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def upsert_member(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: str,
    display_name: str = "",
    all_departments: bool | None = None,
) -> tuple[OrgMember, bool]:
    """Enrol a person by subject, or adopt the row the gate already minted.

    Returns `(member, created)`. `created` is False when the person had already
    reached the system -- their first workspace request mints a row -- and in that
    case the fields given here are applied to it rather than being dropped: an
    administrator who types a display name for somebody who signed in yesterday
    means it, and a POST that silently did nothing would look like a bug in the
    screen.

    `all_departments` is TRI-STATE: `None` leaves it alone, `True` grants it,
    `False` REVOKES it. It used to be a `bool` that only ever widened, which made
    the only endpoint mentioning the flag answer 200 to a request to remove it
    while the flag stayed set -- and `all_departments` outlives the role that
    earned it (`authz/scope.py` ORs the row term into `decide_everywhere` on the
    HTTP path too), so a person demoted in the identity provider kept company-wide
    authority to sign off every approval in the tenant with no way to take it
    back. There is no `False` written anywhere else in `src/`.

    `None` and not a `False` default, because the symmetric mistake is just as
    bad: a POST sent to correct somebody's display name must not strip a CEO of
    his company-wide view as a side effect.
    """
    existing = (
        (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.subject == subject,
                    OrgMember.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if display_name:
            existing.display_name = display_name
        if all_departments is not None:
            existing.all_departments = all_departments
        await db.flush()
        return existing, False

    # Core INSERT ... ON CONFLICT DO NOTHING rather than `db.add()` + flush, and
    # untargeted: `org_member` carries two partial unique indexes and either can
    # fire, while a flush that raises IntegrityError leaves the session needing a
    # rollback -- turning a harmless race with the gate's own upsert into a failed
    # request. Same shape as `authz/scope.py::_upsert_member`, deliberately.
    from oc8.authz.scope import subject_uuid_for

    inserted = (
        await db.execute(
            pg_insert(OrgMember)
            .values(
                id=uuid7(),
                tenant_id=tenant_id,
                subject=subject,
                subject_uuid=subject_uuid_for(subject),
                display_name=display_name,
                # `None` means "unsaid", and an unsaid grant on a row that does
                # not exist yet is no grant: the column is NOT NULL with a `false`
                # server default, so writing the coalesced value keeps the two
                # paths saying the same thing rather than leaning on the default
                # in one of them.
                all_departments=bool(all_departments),
            )
            .on_conflict_do_nothing()
            # RETURNING is how "did I actually insert" is answered. Without it the
            # conflict case -- this administrator's POST racing the same person's
            # own first request, which is exactly when it happens -- reported 201
            # for a row it did not create AND dropped the display name and the
            # grant it was sent with, because the branch below never applied them.
            .returning(OrgMember.id)
        )
    ).first()

    minted = (
        (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.subject == subject,
                    OrgMember.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if minted is None:  # pragma: no cover - unreachable under READ COMMITTED
        raise RuntimeError(
            f"could not mint or re-read org_member for subject {subject!r}; "
            "this transaction is not READ COMMITTED"
        )
    if inserted is None:
        # Somebody else's INSERT won. Apply what this caller asked for to the row
        # that exists, exactly as the `existing is not None` branch above does.
        if display_name:
            minted.display_name = display_name
        if all_departments is not None:
            minted.all_departments = all_departments
        await db.flush()
    return minted, inserted is not None


async def grant_seat(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    department_id: uuid.UUID,
    seat_role: str,
    agent_manage: bool | None = None,
    granted_by: uuid.UUID | None,
) -> tuple[OrgMemberDepartment, bool]:
    """Seat somebody in a department. Returns `(seat, changed)`.

    `agent_manage` is TRI-STATE, the same shape `upsert_member` gives
    `all_departments` above and for the identical reason: `None` means "leave
    unchanged", never "unset it". A plain `bool = False` default was tried and
    rejected in review -- it made an ordinary, unrelated `seat_role` promotion
    or demotion silently STRIP an already-granted write toggle the instant the
    caller omitted the field, because the revoke-and-recreate a role change
    requires has nothing else to carry the old value forward with. Pinned by
    `tests/workspace/test_members.py::
    test_omitting_agent_manage_on_seat_role_change_does_not_reset_it`, which
    calls this with the argument genuinely absent -- not passed as an explicit
    `None` -- the shape a caller who has never heard of the toggle would write.

    A live seat there at the SAME role is updated in place: `agent_manage` is
    resolved (kept if the caller said nothing, overridden if they did) and
    written only if it actually changed. It is not unconditionally reissued --
    that would write a revoke/grant pair into the audit trail for an act
    nobody performed, and would move `granted_by` to whoever last clicked the
    button rather than whoever actually decided.

    A live seat at a DIFFERENT role is revoked and replaced in this same
    transaction, which is also what the partial unique index requires -- two live
    seats for one person in one department is unrepresentable, so a promotion has
    to be a revoke followed by a grant, in that order. `agent_manage` is CARRIED
    FORWARD onto the new row unless the caller explicitly overrides it -- the
    write authority a seat holder was trusted with does not evaporate because an
    administrator changed what she may READ.
    """
    validated_seat_role(seat_role)
    live = (
        (
            await db.execute(
                select(OrgMemberDepartment).where(
                    OrgMemberDepartment.tenant_id == tenant_id,
                    OrgMemberDepartment.member_id == member_id,
                    OrgMemberDepartment.department_id == department_id,
                    OrgMemberDepartment.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if live is None:
        seat = OrgMemberDepartment(
            tenant_id=tenant_id,
            member_id=member_id,
            department_id=department_id,
            seat_role=seat_role,
            agent_manage=bool(agent_manage),
            granted_by=granted_by,
        )
        db.add(seat)
        await db.flush()
        return seat, True

    if live.seat_role == seat_role:
        resolved = live.agent_manage if agent_manage is None else agent_manage
        if resolved == live.agent_manage:
            return live, False
        live.agent_manage = resolved
        live.granted_by = granted_by
        await db.flush()
        return live, True

    live.revoked_at = dt.datetime.now(dt.UTC)
    # Flushed BEFORE the insert below. The unique index is partial on
    # `revoked_at IS NULL` and is checked per statement, so an INSERT issued
    # while the revoke is still pending in the session collides with the row
    # it is replacing.
    await db.flush()

    new_agent_manage = live.agent_manage if agent_manage is None else agent_manage
    seat = OrgMemberDepartment(
        tenant_id=tenant_id,
        member_id=member_id,
        department_id=department_id,
        seat_role=seat_role,
        agent_manage=new_agent_manage,
        granted_by=granted_by,
    )
    db.add(seat)
    await db.flush()
    return seat, True


async def revoke_seat(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, department_id: uuid.UUID
) -> OrgMemberDepartment | None:
    """Take a seat away. `None` when there was no live one to take.

    Effective on the caller's next request, with no token refresh and no TTL wait,
    because a seat is read from this row on every request rather than carried in
    the JWT. That is the whole justification for keeping seats out of the token:
    every alternative means revoking the authority to release money takes effect
    somewhere between now and the next refresh, and nobody can say when.
    """
    live = (
        (
            await db.execute(
                select(OrgMemberDepartment).where(
                    OrgMemberDepartment.tenant_id == tenant_id,
                    OrgMemberDepartment.member_id == member_id,
                    OrgMemberDepartment.department_id == department_id,
                    OrgMemberDepartment.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if live is None:
        return None
    live.revoked_at = dt.datetime.now(dt.UTC)
    await db.flush()
    return live


async def delete_member(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> OrgMember | None:
    """Soft-delete a person. `None` when they are already gone or were never here.

    Stamps `deleted_at` rather than removing the row: the partial unique indexes
    on `(tenant_id, subject)` / `(tenant_id, subject_uuid)` are what let the
    same person be enrolled again as a stranger, and an audit that asked
    "who was this" would otherwise be answered with silence. Login, scope,
    and the members list already filter `deleted_at IS NULL` -- this is the
    write those reads have been waiting for.
    """
    member = await get_member(db, tenant_id=tenant_id, member_id=member_id)
    if member is None:
        return None
    member.deleted_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()
    return member
