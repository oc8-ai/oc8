"""Which departments a HUMAN stands in, and the actor every human funnel takes (§2).

Until this module there was no term in the system in which "his department, and
not the whole company's" could be true. `GET /approvals` filtered on status
alone and handed back every pending approval in the tenant; `POST
/approvals/{id}/decision` checked a permission and never asked whose approval it
was; a person was five claims in a JWT with nowhere to stand.

**Three things this module is deliberately NOT.**

*Not a second RLS GUC.* `tests/db/test_organization_rls_pooled.py:26-38` documents
that once a pooled connection has bound a custom GUC with `is_local=true`, its
post-transaction reset value is `''` and not NULL for the life of that physical
connection -- migration 0015 exists because of exactly this. So "unset means a
system session" is unavailable here, and a department GUC would fail OPEN on
forget, which is the inverse of the property that makes `app.tenant_id`
trustworthy. The term lives in application code, in one funnel and one
repository, guarded by a source sweep.

*Not on the agent path.* `get_db` is untouched. It is `DbSession` in
`mcp_gateway.py`, `llm_gateway.py` and `internal_agent.py`, which is how the
runtime RAISES an approval; binding a scope there would break the 3000-EUR gate
the product is demoed on. Nothing in this module is imported by
`authorize_tool`, the department frame, or agent narrowing.

*Not a permission check.* A scope answers WHERE, never WHAT. `require_departmental`
answers what (does this caller hold `approval:decide` at all), the scope answers
where (in which departments), and `decide_approval` asks the scope again as
defence in depth. Keeping the two apart is why an `auditor` -- unrestricted by
`approval:view_any` -- is still refused at the decide door: he sees every
department and decides in none, which is two flags here and not one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oc8.authz.authority import _role_and_permissions, granted_for_member
from oc8.authz.permissions import (
    APPROVAL,
    APPROVAL_DECIDE,
    APPROVAL_DECIDE_ANY,
    APPROVAL_VIEW_ANY,
    SEAT_APPROVER,
    SEAT_PERMISSIONS,
    SEAT_VIEWER,
    VIEW,
    perm,
    permissions_for,
    seat_permissions_for,
)
from oc8.db.base import uuid7
from oc8.models.channels import ApprovalChannelBinding
from oc8.models.core import Department
from oc8.models.identity import OrgMember, OrgMemberDepartment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from oc8.auth.principal import Principal

logger = logging.getLogger(__name__)

#: `approval:view`. Spelled out here rather than imported because
#: `authz/permissions.py` names only the actions that needed their own constant;
#: the product tuple builds this one.
APPROVAL_VIEW: Final = perm(APPROVAL, VIEW)

#: What a seat carries wherever it sits (both seat roles hold these), and what
#: only an approver seat adds. Derived from `SEAT_PERMISSIONS` rather than
#: restated, so a change to the vocabulary cannot leave this file behind saying
#: something else.
_VIEW_LEVEL: Final[frozenset[str]] = SEAT_PERMISSIONS[SEAT_VIEWER]
_DECIDE_LEVEL: Final[frozenset[str]] = SEAT_PERMISSIONS[SEAT_APPROVER] - _VIEW_LEVEL


def check_two_level_vocabulary(seat_permissions: dict[str, frozenset[str]]) -> None:
    """Refuse a seat vocabulary this module cannot represent.

    A `DepartmentScope` carries TWO department sets -- `_departments` and
    `_decide` -- so it can only answer for a vocabulary in which every seat role
    holds either the view level or the view level plus the whole decide level.
    Nothing said so, and the gap was live though inert: `holds_anywhere` answers
    `clarification:answer` out of `_decide`, which `_scope_from_seats` builds from
    `approval:decide` ALONE. A third seat role carrying `clarification:answer`
    without `approval:decide` would have been refused at
    `require_departmental(CLARIFICATION_ANSWER)` while `SEAT_PERMISSIONS` -- and
    `GET /governance`, which renders it -- said the seat granted it.

    Raised at import, like `require_permission`'s unknown-permission check and for
    the same reason: the vocabulary is a code constant, so this fails in CI on the
    commit that breaks it rather than at 03:00 on a refusal nobody can explain.
    Adding a third seat role means either fitting this shape or teaching the scope
    a third set -- and either way somebody has decided, which is the point.
    """
    for seat_role, granted in seat_permissions.items():
        if granted == _VIEW_LEVEL or granted == _VIEW_LEVEL | _DECIDE_LEVEL:
            continue
        raise RuntimeError(
            f"seat role {seat_role!r} grants {sorted(granted)}, which is neither "
            f"the view level {sorted(_VIEW_LEVEL)} nor that plus the decide level "
            f"{sorted(_DECIDE_LEVEL)}. A DepartmentScope has two department sets "
            "and cannot answer for a third level; see check_two_level_vocabulary."
        )


check_two_level_vocabulary(SEAT_PERMISSIONS)


# --------------------------------------------------------------------- the term


@dataclass(frozen=True, init=False)
class DepartmentScope:
    """Where one person may look, and where they may sign off.

    **The constructor refuses everybody.** `init=False` plus an `__init__` that
    raises is not decoration: the two designs this one replaces both had a frozen
    dataclass with a public constructor, so any door written next year could
    satisfy a required `actor=` keyword with `Scope(frozenset(), unrestricted=True)`
    and mypy would smile at it. A scope is only ever produced by a resolver in
    this module, from rows. `dataclasses.replace` goes through `__init__` too, so
    the innocent-looking way round a frozen dataclass is closed by the same line.

    `_unrestricted` is a BOOLEAN and not "the set of every department". A
    department created after the scope was resolved is inside a CEO's view by
    construction, and no query has to remember to add it -- the failure being
    avoided is silent, because a forgotten set does not error, it just stops
    showing one department's approvals.
    """

    #: SEES everywhere, including departments that do not exist yet.
    _unrestricted: bool
    #: DECIDES everywhere. A strict subset of `_unrestricted`, and the two are
    #: separate for one holder: the `auditor`, who is given `approval:view_any`
    #: and deliberately not `approval:decide_any` (`authz/permissions.py:255`).
    #: Collapsing them into one flag made an auditor `may_decide(anything)` --
    #: i.e. the role whose whole value is that its account cannot have caused
    #: what it is auditing could sign off a 4.320-EUR offer, and the funnel's
    #: defence-in-depth check would have waved it through. Pinned by
    #: `tests/api/test_approvals_department_scope.py::
    #: test_an_auditor_sees_everything_and_may_decide_nothing`.
    _decide_everywhere: bool
    #: Departments where a live seat carries `approval:view`.
    _departments: frozenset[uuid.UUID]
    #: The subset where that seat also carries `approval:decide`.
    _decide: frozenset[uuid.UUID]
    #: Departments where a live seat's `agent_manage` column is TRUE.
    #: Independent of `seat_role` -- never a permission string, never in
    #: `SEAT_PERMISSIONS`, so this set is what `require_agent_write`/
    #: `authorize_agent_write` (`api/deps.py`) read and nothing else does. A
    #: STRUCTURAL subset of `_departments` (see `_scope_from_seats`), not a
    #: coincidence of today's seat vocabulary.
    _agent_manage: frozenset[uuid.UUID]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "a DepartmentScope is resolved from rows, never constructed: use "
            "scope_for_principal() or scope_for_binding(). A scope somebody can "
            "build is a scope somebody can widen."
        )

    # `department_id is None` means TENANT-WIDE -- today exactly one producer, the
    # tenant-scope budget incident in `metering/budget.py`. A seat is not the
    # tenant, so a seat-holder neither sees it nor answers it.

    def may_view(self, department_id: uuid.UUID | None) -> bool:
        if self._unrestricted:
            return True
        if department_id is None:
            return False
        return department_id in self._departments

    def may_decide(self, department_id: uuid.UUID | None) -> bool:
        if self._decide_everywhere:
            return True
        if department_id is None:
            return False
        return department_id in self._decide

    def holds_anywhere(self, permission: str) -> bool:
        """Does this person hold `permission` in at least one department?

        The door's question, and the reason it is not `permission in
        seat_permissions_for(...)`: `require_departmental` has to admit a caller
        with a seat SOMEWHERE before any row is loaded, and only then narrow.

        Fail-closed on anything outside the seat vocabulary. Without that first
        branch an unrestricted caller would "hold" `run:control` here -- and the
        day somebody writes `require_departmental(RUN_CONTROL)` that is a
        tenant-wide grant handed out by a gate whose name says departmental.

        And answered from the DECIDE flag for a decide-level permission, not from
        "unrestricted" as such: an `auditor` is unrestricted (he holds
        `approval:view_any`) and holds `approval:decide` nowhere, so a single flag
        here admitted him at `POST /approvals/{id}/decision` -- the one door the
        role exists to be kept away from.
        """
        if permission not in _VIEW_LEVEL and permission not in _DECIDE_LEVEL:
            return False
        if permission in _DECIDE_LEVEL:
            return self._decide_everywhere or bool(self._decide)
        return self._unrestricted or bool(self._departments)

    @property
    def is_empty(self) -> bool:
        """Holds nothing anywhere -- the "you are not in any department yet"
        state, which the screen must be able to tell apart from "nothing is
        waiting for you". Those two are the same blank page today, and "the
        system is broken" must not look like "nobody has added me yet".

        Both sets are read although `_decide` is a subset of `_departments` for
        every seat role that exists: the subset relation is a property of
        `SEAT_PERMISSIONS`, not of this class, and a scope that says it is empty
        while `may_decide` answers True would be the worst of the two lies.
        """
        return not self._unrestricted and not self._departments and not self._decide

    @property
    def is_unrestricted(self) -> bool:
        """Read this BEFORE `viewable` when building a query.

        `viewable` is empty for an unrestricted caller -- there is no set of every
        department by design -- so a filter that reads `viewable` alone shows a
        CEO nothing, and a filter that skips itself when `viewable` is empty shows
        a seatless employee everything. One of those two is a data breach, which
        is why both callers go through `oc8.approvals.repo`.
        """
        return self._unrestricted

    @property
    def decides_everywhere(self) -> bool:
        """Signs off everywhere, not merely SEES everywhere.

        On the wire (`GET /me`) because the two flags are not the same person and
        the screen could not tell them apart: `MeDTO` carried `is_unrestricted`
        alone, and `workspace.tsx` therefore drew Approve and Reject for an
        `auditor` -- who holds `approval:view_any`, is unrestricted, and is 403'd
        by `require_departmental(approval:decide)` on every click. The whole value
        of that role is that its account cannot have caused what it is auditing;
        offering it the button was the UI contradicting the model this module
        keeps two flags for.
        """
        return self._decide_everywhere

    @property
    def viewable(self) -> frozenset[uuid.UUID]:
        """The departments for an `IN (...)`. Empty when unrestricted; see above."""
        return self._departments

    def may_manage_agents(self, department_id: uuid.UUID | None) -> bool:
        """WRITE authority for Agent, in exactly one department.

        Deliberately NOT folded into `_unrestricted` (that is `approval:view_any`,
        an AUDITOR's flag) or into `may_view` (a READ question) -- either would
        let an auditor, or a plain `dept_viewer` with no toggle, reach
        `agent:manage` nobody granted them. There is no tenant-wide analogue of
        this method on this class: the tenant-wide term (`agent:manage` in
        `Authority.tenant_wide`) is checked separately, by `authorize_agent_write`
        itself, because a scope only ever answers WHERE a SEAT reaches -- see the
        module docstring's "not a permission check".
        """
        if department_id is None:
            return False
        return department_id in self._agent_manage

    @property
    def agent_manage_departments(self) -> frozenset[uuid.UUID]:
        """Every department where a live seat's `agent_manage` is TRUE. Empty for
        an unrestricted caller too -- there is no tenant-wide member of this set,
        same reasoning as `viewable`: `require_agent_write`'s tenant-wide branch
        is a SEPARATE check against `Authority.tenant_wide`, not this property."""
        return self._agent_manage


def _resolved_scope(
    *,
    unrestricted: bool,
    decide_everywhere: bool,
    departments: frozenset[uuid.UUID],
    decide: frozenset[uuid.UUID],
    agent_manage: frozenset[uuid.UUID] = frozenset(),
) -> DepartmentScope:
    """The only way a `DepartmentScope` comes into existence.

    Module-private and a free function rather than a classmethod: a
    `DepartmentScope.build(...)` on the public class surface is an invitation
    every IDE would offer, and the point of the private constructor is that
    reaching for this is visibly reaching for somebody else's private.
    """
    scope = object.__new__(DepartmentScope)
    # Deciding everywhere without seeing everywhere is not a state any resolver
    # can produce, and a scope in it would answer `may_decide` for a department
    # `may_view` denies. Asserted rather than trusted because the two flags are
    # set by three call sites.
    if decide_everywhere and not unrestricted:
        raise ValueError("a scope that decides everywhere must also see everywhere")
    object.__setattr__(scope, "_unrestricted", unrestricted)
    object.__setattr__(scope, "_decide_everywhere", decide_everywhere)
    object.__setattr__(scope, "_departments", departments)
    object.__setattr__(scope, "_decide", decide)
    object.__setattr__(scope, "_agent_manage", agent_manage)
    return scope


_EMPTY = _resolved_scope(
    unrestricted=False,
    decide_everywhere=False,
    departments=frozenset(),
    decide=frozenset(),
    agent_manage=frozenset(),
)


# ------------------------------------------------------------------- the actors


@dataclass(frozen=True)
class HumanActor:
    """Somebody who arrived with a token, resolved once at the gate.

    Frozen because the scope is the authorization: a route that could assign
    `actor.scope` after `require_departmental` resolved it would put the door
    back where it was.
    """

    principal: Principal
    member: OrgMember
    scope: DepartmentScope
    via: str = "inbox"


@dataclass(frozen=True)
class ChannelActor:
    """Somebody who answered from a messenger, where there is no token at all.

    `principal` is absent rather than faked -- a Telegram message is not an
    authentication event, and inventing a Principal here is how `actor_id` in the
    audit trail would start naming somebody who never signed in. Attribution comes
    off `member`, which is why a binding with `member_id IS NULL` decides nothing.
    """

    binding: ApprovalChannelBinding
    member: OrgMember
    scope: DepartmentScope
    via: str


@dataclass(frozen=True)
class AgentActor:
    """Somebody a Copilot tool is acting FOR -- resolved from the chat run
    behind the call, with no live Principal and no channel binding.

    The third door `decide_approval` (and any future write-capable Copilot
    tool) is called through: not a token (`HumanActor`), not a messenger
    binding (`ChannelActor`), but the human on the other end of a chat
    session the tool call is running inside of. `scope` is resolved the
    same way `control_tools._member_may_reach_department` already resolves
    it for `delegate_task` -- `scope_for_member`, off the SAME member row --
    so a Copilot tool can never see or decide more than the human behind it
    could through any other door.
    """

    member: OrgMember
    scope: DepartmentScope
    via: str = "copilot"


#: What every human funnel takes. `decide_approval(..., *, actor: DecisionActor)`
#: is a required keyword with no default, so a door written next year is a
#: TypeError at call time and a mypy error in CI rather than a silent bypass.
DecisionActor = HumanActor | ChannelActor | AgentActor


# ---------------------------------------------------------------- the resolvers


def subject_uuid_for(subject: str) -> uuid.UUID:
    """A person as a uuid, derived exactly as `api/v1/channels.py::_subject_id`
    derives `ApprovalChannelBinding.user_id`.

    The two derivations MUST agree: the messenger door has no token, so the only
    way back from a binding to a person is this value. `tests/channels/
    test_channel_decision_is_scoped.py` asserts they still do.
    """
    try:
        return uuid.UUID(subject)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


async def _member_by_subject(
    db: AsyncSession, tenant_id: uuid.UUID, subject: str
) -> OrgMember | None:
    stmt = select(OrgMember).where(
        # RLS already scopes this, but the predicate is written out because the
        # value is also what the uniqueness is on: reading a member by subject
        # ALONE would, on an unbound session, be a lookup across every tenant.
        OrgMember.tenant_id == tenant_id,
        OrgMember.subject == subject,
        # The same filter `authz.authority.resolve_authority` applies, and it has
        # to stay the same one. A soft-deleted row is invisible to BOTH, so both
        # answer "the token decides" -- and because `_upsert_member` then mints a
        # fresh row for that subject, a resolver taught to see the deleted row
        # and refuse would disagree with this door on the same request. That is
        # the deferred offboarding item (design residual 9), and it is one
        # decision about both readers rather than a fix to either.
        OrgMember.deleted_at.is_(None),
    )
    return (await db.execute(stmt)).scalars().first()


async def _upsert_member(
    db: AsyncSession, tenant_id: uuid.UUID, subject: str, email: str | None = None
) -> OrgMember:
    """SELECT, then INSERT ... ON CONFLICT DO NOTHING, then SELECT again (§1).

    Three properties, each one a bug that has bitten this repository:

    * **DO NOTHING and never DO UPDATE.** `DO UPDATE` takes a row lock on EVERY
      request from an existing person, even when its `WHERE` is false, so two tabs
      belonging to one person would serialise against each other for the length of
      the slower one's whole route body -- every time, for ever. `DO NOTHING` also
      waits on a concurrent inserter's transaction while the conflicting tuple is
      uncommitted, so it is not lock-free either; the difference is that it can
      only happen on a person's genuinely first two simultaneous requests and
      never again.
    * **No COMMIT.** A commit inside a `tenant_session` unbinds `app.tenant_id`
      for the rest of that session, and every statement after it silently sees
      nothing. This runs in `require_departmental`, i.e. BEFORE the route body,
      so a commit here would empty the approvals list with no error anywhere.
    * **Core insert, not `db.add()` + flush.** A flush that raises IntegrityError
      leaves the session needing rollback, so the caller's next statement dies
      with PendingRollbackError -- turning a harmless race into a failed request.

    The token's `email` claim, when there is one, is copied onto the row only
    when it differs, so a returning person's request writes nothing and takes
    no row lock. It is display data and never part of the lookup.
    """
    existing = await _member_by_subject(db, tenant_id, subject)
    if existing is not None:
        if email and existing.email != email:
            existing.email = email
        return existing

    await db.execute(
        pg_insert(OrgMember)
        .values(
            id=uuid7(),
            tenant_id=tenant_id,
            subject=subject,
            subject_uuid=subject_uuid_for(subject),
            email=email,
        )
        # Untargeted on purpose: `org_member` carries TWO partial unique indexes
        # (subject and subject_uuid) and either can be the one that fires.
        # Naming one would restate its `WHERE deleted_at IS NULL` predicate here,
        # a second copy that stops matching the day the index changes -- and a
        # DO NOTHING that infers no index raises instead of doing nothing.
        .on_conflict_do_nothing()
    )

    minted = await _member_by_subject(db, tenant_id, subject)
    if minted is None:
        # Unreachable under READ COMMITTED, which is what this database runs: a
        # concurrent inserter's row is visible to the re-read the moment its
        # transaction ends, and ON CONFLICT DO NOTHING waited for exactly that.
        # Named rather than assumed, because the silent alternative -- returning
        # None -- would hand the gate a caller with no member and write NULL into
        # `approval_request.decided_by`.
        raise RuntimeError(
            f"could not mint or re-read org_member for subject {subject!r}; "
            "this transaction is not READ COMMITTED"
        )
    return minted


async def _scope_from_seats(
    db: AsyncSession, member: OrgMember | None, *, unrestricted: bool, decide_everywhere: bool
) -> DepartmentScope:
    if member is None:
        return _resolved_scope(
            unrestricted=unrestricted,
            decide_everywhere=decide_everywhere,
            departments=frozenset(),
            decide=frozenset(),
        )

    rows = (
        await db.execute(
            select(
                OrgMemberDepartment.department_id,
                OrgMemberDepartment.seat_role,
                OrgMemberDepartment.agent_manage,
            )
            # INNER, and on `deleted_at IS NULL`. There is no foreign key here --
            # a seat outlives an archived department on purpose, so the members
            # screen can still show and revoke it -- but AUTHORITY must not: this
            # read never joined `Department` at all, so archiving a department
            # narrowed nobody, and a seat naming a department that does not exist
            # (there is no FK to stop one) granted a place to stand that no screen
            # could show. `PUT /members/{id}/departments/{id}` already refuses to
            # seat anybody in an archived department; this is the same position,
            # held on the read side too.
            #
            # The cost is named rather than discovered: pending approvals in an
            # archived department become answerable only by the unrestricted. That
            # is the fail-closed direction, and they are still visible to them.
            .join(Department, Department.id == OrgMemberDepartment.department_id)
            .where(
                OrgMemberDepartment.tenant_id == member.tenant_id,
                OrgMemberDepartment.member_id == member.id,
                # The seat table keeps revoked rows so an audit can answer "who
                # could approve this, and until when". Reading every row for the
                # member -- the obvious query -- keeps a revoked approver
                # deciding for ever.
                OrgMemberDepartment.revoked_at.is_(None),
                Department.tenant_id == member.tenant_id,
                Department.deleted_at.is_(None),
            )
        )
    ).all()

    departments: set[uuid.UUID] = set()
    decide: set[uuid.UUID] = set()
    agent_manage: set[uuid.UUID] = set()
    for department_id, seat_role, may_manage in rows:
        # Through `seat_permissions_for` rather than `seat_role == SEAT_APPROVER`:
        # the vocabulary is defined in exactly one place, and an unrecognised
        # seat_role (a future migration this code predates) carries nothing
        # instead of being read as the more powerful of the two.
        carried = seat_permissions_for(seat_role)
        # Gated on `carried or may_manage`, not `carried` alone: `agent_manage`
        # rides a column that is INDEPENDENT of `seat_role`, so nothing stops a
        # future seat vocabulary from defining a role that carries neither
        # `approval:view` nor `agent:view`. Without this, `agent_manage ⊆
        # viewable` would be a coincidence of today's two seat roles (both of
        # which happen to carry the view level) rather than a structural
        # property `authorize_agent_write` can lean on.
        if APPROVAL_VIEW in carried or may_manage:
            departments.add(department_id)
        if APPROVAL_DECIDE in carried:
            decide.add(department_id)
        if may_manage:
            agent_manage.add(department_id)

    return _resolved_scope(
        unrestricted=unrestricted,
        decide_everywhere=decide_everywhere,
        departments=frozenset(departments),
        decide=frozenset(decide),
        agent_manage=frozenset(agent_manage),
    )


async def scope_for_principal(
    db: AsyncSession, principal: Principal, *, upsert: bool = False
) -> tuple[OrgMember | None, DepartmentScope]:
    """Where this token's holder stands.

    `upsert=True` mints the member row on first sight, which is what makes
    `decided_by` never NULL and what lets `POST /members` offer subjects the
    system has actually seen instead of asking an admin to type an id by hand.
    Read-only callers pass `upsert=False` and get `(None, empty)` for a stranger.

    Refused for any principal whose `kind` is not `"operator"`. By the KIND and
    not by hoping `role="plugin"` stays unknown: an agent token has a `sub` too,
    and a resolver that looked people up by `sub` alone would happily find a
    member row for the container -- which is the container approving its own
    >3000 EUR call, a hole that has already been reachable once on this system
    (`deny_agent_principals`, verified 2026-07-27).

    `PermissionError` rather than an `assert`: `python -O` strips asserts, and an
    authorization check a compiler flag removes is not a check.
    """
    if principal.kind != "operator":
        raise PermissionError("only an operator principal stands in a department")

    if upsert:
        member: OrgMember | None = await _upsert_member(
            db, principal.tenant_id, principal.subject, principal.email
        )
    else:
        member = await _member_by_subject(db, principal.tenant_id, principal.subject)

    # One rule, and the two terms are not interchangeable. The RESOLVED term is
    # available only here (the messenger door has no principal at all); the ROW
    # term is available at both doors, written at an authenticated moment by
    # `POST /channels/{channel}/link` and revocable -- never inferred from a
    # phone number. `approval:view_any` / `approval:decide_any` are separate
    # permissions rather than a property of a role NAME because a seat may carry
    # `approval:view`/`approval:decide`: if holding those meant "anywhere", every
    # seat would be tenant-wide and this whole module would be decoration.
    #
    # Split in two because the vocabulary is: `approval:view_any` is given to
    # `auditor` and `approval:decide_any` is not, and a single flag would have
    # made "read the whole company's approvals" and "sign the whole company's
    # approvals off" the same grant.
    #
    # RESOLVED, not `role_has(principal.role, ...)`. Both flags are the words "in
    # every department", and read off the token they are the one thing an
    # assignment would not have taken away: an administrator demoted to a role
    # holding four permissions would still have SEEN and DECIDED every
    # department's approvals, i.e. the demotion would have applied to every
    # screen except the one that releases money. Neither `_any` permission is
    # delegatable, so a tenant-defined role can never set these -- only the code
    # floor or an assigned built-in can.
    granted, _source = await granted_for_member(db, principal, member)
    everywhere_row = member is not None and member.all_departments
    decide_everywhere = APPROVAL_DECIDE_ANY in granted or everywhere_row
    unrestricted = APPROVAL_VIEW_ANY in granted or decide_everywhere
    return member, await _scope_from_seats(
        db, member, unrestricted=unrestricted, decide_everywhere=decide_everywhere
    )


async def scope_for_binding(
    db: AsyncSession, binding: ApprovalChannelBinding
) -> tuple[OrgMember | None, DepartmentScope]:
    """Where the person behind a messenger binding stands.

    Returns `(None, empty)` -- not an exception -- for a binding with no member,
    a member that has been removed, or a member in another tenant. The caller
    (`channels/dispatch.decision_from`) turns every one of those into the SAME
    one-sentence refusal it already gives an unknown sender: an error that tells
    the two apart makes the bot a probe for which approvals exist.

    A binding with `member_id IS NULL` decides nothing. Those are the rows
    migration 0046 deliberately did not grandfather -- inventing an `org_member`
    per binding would collide with `uq_org_member_subject_uuid` the first time
    that same human authenticated, 500-ing every request from exactly the
    population piloting the feature. They are re-issued through
    `POST /channels/{channel}/link`.

    Only the ROW term is available here. There is no token to carry
    `approval:decide_any`, so a CEO deciding from his phone is unrestricted
    because `org_member.all_departments` says so, and for no other reason.
    """
    if binding.member_id is None:
        return None, _EMPTY

    member = (
        (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.tenant_id == binding.tenant_id,
                    OrgMember.id == binding.member_id,
                    OrgMember.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if member is None:
        logger.warning(
            "approval channel binding %s names a member that is not readable here", binding.id
        )
        return None, _EMPTY

    return member, await _scope_from_seats(
        db,
        member,
        unrestricted=member.all_departments,
        decide_everywhere=member.all_departments,
    )


async def scope_for_member(
    db: AsyncSession, member: OrgMember, *, token_role: str | None = None
) -> DepartmentScope:
    """Where a person stands, resolved OFF-REQUEST -- from a stored member row
    rather than from a live token.

    The third door onto the same question. `scope_for_principal` answers it for
    an HTTP caller and `scope_for_binding` for a messenger sender; this one
    answers it for code holding nothing but an `org_member.id` -- an agent run
    asking, mid-run, what the human who started it could have reached
    (`agent/control_tools.py`'s `_member_may_reach_department`). That guard used
    to hand-roll its own answer as "`all_departments`, or a live seat in exactly
    this department", which is the ROW term only: on a fresh tenant `_upsert_member`
    mints members with neither, so the honest answer there was "nobody may reach
    anything", and cross-department delegation was dead for everyone whose
    authority came from a role.

    Three terms, in the same order and with the same meaning as
    `scope_for_principal`:

    * an ASSIGNED role's `approval:view_any` / `approval:decide_any`, read out
      of the role table rather than off a token, because that is the term an
      administrator can grant and revoke;
    * `all_departments` on the row;
    * live seats.

    `token_role` is the one term that CANNOT be recovered from storage -- for a
    member with no assigned role the resolved answer is `permissions_for(token.role)`,
    and there is no token here. A caller that captured it at an authenticated
    moment (the chat API writes it into the run's context) may pass it; a caller
    that has none passes nothing and gets the row terms alone, which is the
    fail-closed direction and exactly what `scope_for_binding` already does at
    the messenger door.
    """
    if member.deleted_at is not None:
        # An offboarded person's stored rows must not keep authorising anything
        # -- the same predicate `channels/binding.recipients()` applies before
        # telling somebody about an approval.
        return _EMPTY
    if member.role_id is not None:
        _role, granted = await _role_and_permissions(db, member.role_id)
    elif token_role is not None:
        granted = permissions_for(token_role)
    else:
        granted = frozenset()
    decide_everywhere = APPROVAL_DECIDE_ANY in granted or member.all_departments
    unrestricted = APPROVAL_VIEW_ANY in granted or decide_everywhere
    return await _scope_from_seats(
        db, member, unrestricted=unrestricted, decide_everywhere=decide_everywhere
    )
