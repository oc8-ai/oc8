"""What may THIS PERSON do -- resolved once, from the token and from one row.

This module is the single seam through which a database-backed role reaches an
authorization decision. Everything else in `authz/` answers a question about a
NAME (`permissions_for("operator")`, `tool_rights_for_role`, `seat_permissions_for`);
this is the only thing that answers a question about a caller.

**The rule, and it is one sentence: a row an administrator deliberately wrote
decides; the absence of a row decides nothing.**

```
no member row, or role_id IS NULL   ->  permissions_for(token.role)   # CODE, unchanged
role_id present                     ->  that role's permissions       # the admin said so
```

`org_member.role_id` is an OVERRIDE, not a union term. Both halves of that
matter and they fail in opposite directions:

* A UNION could not demote anybody. `POST /auth/setup` mints `org_admin` and no
  caller has ever passed anything else, so every human on every live tenant holds
  all 52 permissions -- and under a union, assigning "Praktikant" to one of them
  is a no-op the screen reports as a success.
* A REPLACEMENT of the model would lock everyone out the day the rows are
  missing, which is exactly what `permissions.py`'s doctrine is about. So the
  missing-row case is not merely handled, it is byte-identical to today: the same
  frozenset out of `BUILTIN_ROLE_PERMISSIONS`, reached without a role lookup.

The third case has no symmetric partner and is the one worth staring at: a row
that is PRESENT but UNUSABLE -- soft-deleted, agent-kind, or dangling -- resolves
to the empty set and must NOT fall back to the token floor. Falling back would
mean deleting a role silently restored forty demoted people to `org_admin`,
which is the one failure nobody would notice until it was audited.

**Nothing here is cached beyond one request.** No snapshot, no Redis, no TTL.
`request.state` holds the answer for the life of the request, so a route carrying
two gates pays for one lookup and the next request re-reads. That is what makes
"revoking a role takes effect on the next click" true, and it is the property
that lets authority over money stay out of the JWT.

**Nothing here writes.** `require_permission` guards 110 routes including plain
GETs; a resolver that minted a member row would turn every read in the system
into a write, and would leave a row behind for a caller it then refused. Minting
a person is `scope_for_principal(upsert=True)`'s job, at the doors that already
did it.

**Nothing here is on the agent path.** `permissions_for` and
`tool_rights_for_role` stay synchronous, tenant-less and code-only, and neither
imports this module -- `pdp.agent_tool_rights` reaches the code table through
them, so teaching those to read rows would hand an agent tenant-authored grants
for free. The symmetric half of that guard lives here: `role_permissions`
refuses any row whose `kind` is not `'human'`, so an agent-kind row can never
resolve for a person either.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import select

from oc8.authz.permissions import (
    ALL_PERMISSIONS,
    APPROVAL_DECIDE_ANY,
    APPROVAL_VIEW_ANY,
    DELEGATABLE_PERMISSIONS,
    permissions_for,
)
from oc8.models.core import Role, RolePermission
from oc8.models.identity import OrgMember

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.requests import Request

    from oc8.auth.principal import Principal

logger = logging.getLogger(__name__)

#: The `role.kind` a person may resolve. Agent rows live in the same table, and
#: they are refused here by the column rather than filtered out by the query, so
#: a row that arrives by restore or by psql is refused on the same line.
HUMAN: Final = "human"

#: Where the caller's tenant-wide set came from. On the wire in `GET /governance`,
#: because "you hold four permissions" and "you hold four permissions BECAUSE an
#: administrator assigned you Freigabe Vertrieb" are different sentences and only
#: one of them tells somebody who to ask.
AuthoritySource = Literal["token", "assigned"]

#: Where the memo lives: on `request.state`, i.e. on the ASGI scope, so it dies
#: with the request. There is deliberately no process-wide cache.
_MEMO: Final = "oc8_authority"


@dataclass(frozen=True)
class Authority:
    """One caller's authority, resolved from the token and from rows.

    Frozen, because this IS the authorization: a gate that could be handed an
    `Authority` and then have `tenant_wide` widened underneath it is not a gate.
    Unlike `DepartmentScope` the constructor is public -- there is no "everywhere"
    set a caller could invent here, every field is a frozenset or a boolean, and
    both gates read one rather than build one.
    """

    #: What this person may do ANYWHERE in the tenant. The token floor, or the
    #: assigned role's set -- never their union.
    #:
    #: **There is deliberately no seat map here.** Slice 1's seats say WHERE, and
    #: the only door that asks about them -- `require_departmental` -- asks the
    #: `DepartmentScope` it is about to hand the route body, so that the door and
    #: the row filter cannot come to different answers. This object carried a
    #: `by_department` mapping for one release; it was read by nothing, and
    #: filling it made every one of the 110 permission-gated requests run a
    #: three-table LEFT JOIN over `org_member_department` and `department` to
    #: build a dictionary that was then thrown away. What is left is the single
    #: indexed row the design's cost argument actually claims.
    tenant_wide: frozenset[str]
    #: Sees every department, including ones created after this was resolved.
    #: Computed from the RESOLVED set and from `org_member.all_departments`, never
    #: from the token: `approval:view_any` is the words "in every department", and
    #: read off the token it is the one thing a demotion would not take away.
    unrestricted: bool
    #: Decides everywhere. A strict sub-state of `unrestricted`, and separate for
    #: exactly one holder: the `auditor`, who is given `approval:view_any` and
    #: deliberately not `approval:decide_any`.
    decides_everywhere: bool
    #: The person's row, if this tenant has ever seen them. `None` is ordinary
    #: rather than an error -- this resolver never mints.
    member: OrgMember | None = None
    source: AuthoritySource = "token"
    #: The assigned role row, when there is one and it can grant. Carried so the
    #: screen that explains a refusal can NAME the role without a second query.
    #: `None` whenever the source is the token, and also when the assignment
    #: points at a row that grants nothing (deleted, agent-kind, missing) -- a
    #: name shown next to an empty set would read as though the role were empty
    #: rather than unusable.
    role: Role | None = field(default=None, repr=False)


#: A caller with no human authority at all. Returned for any principal that is
#: not an operator -- and returned from a check on `kind`, not from
#: `permissions_for("plugin")` happening to be empty.
_EMPTY: Final = Authority(
    tenant_wide=frozenset(),
    unrestricted=False,
    decides_everywhere=False,
)


# --------------------------------------------------------------- the role's set


async def role_permissions(db: AsyncSession, role_id: uuid.UUID) -> frozenset[str]:
    """What the role row `role_id` grants a person. Two resolvers, chosen by
    `builtin` -- the same shape `pdp.agent_tool_rights` already uses.

    Four ways this returns nothing, and each is a refusal rather than an
    accident:

    * the row does not exist -- a dangling assignment means the deployment does
      not know what this person may do, and guessing "whatever they used to have"
      is the wrong direction;
    * it is soft-deleted -- `SoftDeleteMixin` adds no query filter, so this check
      is the only thing standing between a deleted role and a role that still
      grants;
    * its `kind` is not `'human'` -- an agent role assigned to a person grants
      that person nothing, the mirror of the `pdp.py` guard that stops a human
      role granting an agent tool rights;
    * it is `builtin` and its NAME is not one the code table knows -- a
      hand-written builtin row cannot invent a role.

    The intersection with `DELEGATABLE_PERMISSIONS` runs HERE, at resolve time,
    and not only at the endpoint that writes the rows. A grant can also arrive by
    restore, by psql, or from an importer written next year, and none of those
    passes a validator -- so a row naming `plugin:manage` is inert rather than
    dangerous. It is also what makes a permission graduating OUT of the
    delegatable set go inert on every existing role's next request, with no
    backfill for anybody to remember.
    """
    _role, granted = await _role_and_permissions(db, role_id)
    return granted


async def _role_and_permissions(
    db: AsyncSession, role_id: uuid.UUID
) -> tuple[Role | None, frozenset[str]]:
    """The row and its set together, in ONE statement.

    Two reads of one role is one read too many, and so is a second round trip for
    the grants: this runs on every request made by every person an administrator
    has assigned a role to, which is the entire population this feature invents.
    The LEFT JOIN means a role with no grants still yields its row, which is a
    real and ordinary state -- an administrator composes the role before he ticks
    anything.

    The join is evaluated even for a `builtin` row whose grants are then read from
    code. That is deliberate rather than an oversight: branching first would need
    the role row, i.e. a statement, and the rows a built-in can join to are
    exactly the inert ones this function refuses to read anyway (there are none on
    any live tenant, and a hand-inserted one is ignored two lines below).
    """
    rows = (
        await db.execute(
            select(Role, RolePermission.permission)
            .outerjoin(
                RolePermission,
                # `tenant_id` as well as `role_id`. RLS scopes both sides already;
                # the predicate is written out because `role_id` is what the
                # uniqueness is on, and a join by `role_id` alone on an unbound
                # session would cross tenants.
                (RolePermission.role_id == Role.id) & (RolePermission.tenant_id == Role.tenant_id),
            )
            .where(Role.id == role_id)
        )
    ).all()

    if not rows:
        logger.warning(
            "a member references role %s, which is not readable in this tenant; granting nothing",
            role_id,
        )
        return None, frozenset()

    role: Role = rows[0][0]
    if role.deleted_at is not None or role.kind != HUMAN:
        logger.warning(
            "role %s is %s, so it grants a person nothing",
            role_id,
            "deleted" if role.deleted_at is not None else f"kind={role.kind!r}",
        )
        return None, frozenset()
    if role.builtin:
        # CODE, by name. A `role_permission` row written against a built-in row is
        # inert by design: somebody with psql must not be able to hand `operator`
        # the audit trail by inserting one line.
        return role, permissions_for(role.name)

    granted = frozenset(permission for _role, permission in rows if permission is not None)
    return role, granted & ALL_PERMISSIONS & DELEGATABLE_PERMISSIONS


async def _authority_of_member(
    db: AsyncSession, principal: Principal, member: OrgMember | None
) -> tuple[frozenset[str], AuthoritySource, Role | None]:
    """Decision B, in one place: does a row override the token, or not."""
    if principal.kind != "operator":
        # By the KIND, and raised rather than answered: an agent token and a
        # plugin token both carry a `sub`, so a resolver that looked people up by
        # subject alone would find a member row for a container.
        raise PermissionError("only an operator principal holds a person's authority")
    if member is None or member.role_id is None:
        # THE FLOOR. Byte-identical to what every gate in this system did before
        # this module existed, and reached by every tenant that has configured
        # nothing -- which, on the day this lands, is all of them.
        return permissions_for(principal.role), "token", None
    role, granted = await _role_and_permissions(db, member.role_id)
    return granted, "assigned", role


async def granted_for_member(
    db: AsyncSession, principal: Principal, member: OrgMember | None
) -> tuple[frozenset[str], AuthoritySource]:
    """The override rule for a member somebody else has already resolved.

    `authz/scope.py` needs this answer for a member it may have just minted, and
    `POST /channels/{channel}/link` needs it for the member it is about to write
    a durable grant onto. Both would otherwise reach for
    `role_has(principal.role, ...)` -- which is the token, i.e. the thing the
    assignment was supposed to replace.
    """
    granted, source, _role = await _authority_of_member(db, principal, member)
    return granted, source


# ----------------------------------------------------------------- the resolver


async def resolve_authority(db: AsyncSession, principal: Principal) -> Authority:
    """Read this caller's authority. No memo, no upsert, no writes.

    ONE indexed single-row SELECT for a caller nobody has assigned anything to,
    which on the day this landed is every caller on every tenant; a second
    statement for a caller who has an assignment, joining the role to its grants.
    That is the whole per-request cost, it is what the design claims, and
    `test_the_gate_costs_one_statement_and_an_assignment_costs_two` counts every
    statement rather than the ones that mention a table, because a cost argument
    a test cannot see is a cost argument nobody will keep.

    It used to be three, one of them a three-table LEFT JOIN over the seat tables
    that filled an `Authority.by_department` mapping no module in `src/` read.
    The seats are read where they are used: `require_departmental` asks the
    `DepartmentScope` it hands the route body, so the door and the row filter
    cannot disagree about which departments somebody sits in.
    """
    if principal.kind != "operator":
        return _EMPTY

    member = (
        (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.tenant_id == principal.tenant_id,
                    OrgMember.subject == principal.subject,
                    # `deleted_at IS NULL`, matching `scope._member_by_subject`
                    # exactly. The two must agree: `scope_for_principal(upsert=True)`
                    # MINTS a fresh row for a subject whose row is soft-deleted, so
                    # a resolver that read the deleted row instead would answer one
                    # thing at `require_permission` and another at
                    # `require_departmental`. See the design's residual list, item
                    # 9 -- offboarding has to decide both in one place.
                    OrgMember.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )

    granted, source, role = await _authority_of_member(db, principal, member)

    # The two "everywhere" terms, computed from the RESOLVED set and from the row
    # -- never from `principal.role`. Split in two because the vocabulary is:
    # `auditor` is given `approval:view_any` and deliberately not
    # `approval:decide_any`, and one flag would make "read the whole company's
    # approvals" and "sign them off" the same grant.
    everywhere_row = member is not None and member.all_departments
    decides_everywhere = APPROVAL_DECIDE_ANY in granted or everywhere_row
    unrestricted = APPROVAL_VIEW_ANY in granted or decides_everywhere

    return Authority(
        tenant_wide=granted,
        unrestricted=unrestricted,
        decides_everywhere=decides_everywhere,
        member=member,
        source=source,
        role=role,
    )


async def authority_for_member(
    db: AsyncSession, member: OrgMember, *, token_role: str | None = None
) -> Authority:
    """`resolve_authority`'s tail, for a caller that already has the row.

    The Copilot's write-capable tools (`agent/control_tools.py`'s
    `AgentActor` seam) resolve the human behind a chat turn from a run, not
    from a live request -- there is no `Principal` or `Request` to memoise
    against. This is that resolver's off-request twin: same two-case
    doctrine (an assigned role replaces the token floor; its absence falls
    back to the token, never to nothing), same seat independence (a seat
    grant is read from `DepartmentScope.holds_anywhere`, never from here),
    no memoisation (nothing here runs more than once per tool call), no
    `Principal`-kind guard (an `AgentActor.member` is already known-human --
    `_resolve_agent_actor` only ever produces one from a real `OrgMember`
    row).
    """
    if member.role_id is None:
        granted = permissions_for(token_role) if token_role is not None else frozenset()
        source: AuthoritySource = "token"
        role = None
    else:
        role, granted = await _role_and_permissions(db, member.role_id)
        source = "assigned"

    decides_everywhere = APPROVAL_DECIDE_ANY in granted or member.all_departments
    unrestricted = APPROVAL_VIEW_ANY in granted or decides_everywhere

    return Authority(
        tenant_wide=granted,
        unrestricted=unrestricted,
        decides_everywhere=decides_everywhere,
        member=member,
        source=source,
        role=role,
    )


async def authority_for_principal(
    request: Request, db: AsyncSession, principal: Principal
) -> Authority:
    """One resolver, both gates, memoised for the life of ONE request.

    The memo lives on `request.state` and nowhere else. A route carrying
    `require_permission` and `require_departmental` therefore pays for one lookup
    rather than two, and the next request pays again -- which is the whole of the
    invalidation story. §5.4's snapshot cache is deliberately not built: it would
    put a staleness window between "the administrator revoked the role" and "the
    caller stops being able to release money", and there is no window worth having
    there.
    """
    cached = getattr(request.state, _MEMO, None)
    if isinstance(cached, Authority):
        return cached
    resolved = await resolve_authority(db, principal)
    setattr(request.state, _MEMO, resolved)
    return resolved


def tenant_wide_read(authority: Authority, permission: str) -> bool:
    """Whether `permission` is held WITHOUT department scope -- i.e. by a
    genuinely tenant-wide grant -- rather than merely appearing in `authority.
    tenant_wide`.

    This exists because `perm(AGENT, VIEW)`/`perm(DEPARTMENT, VIEW)` are
    DELEGATABLE (department-scoped-agent-authority design, migration 0048): a
    tenant-defined role built in the role builder may now hold either string.
    `authority.tenant_wide` is a flat frozenset and cannot tell "org_admin holds
    this, unconditionally, same as before this permission was delegatable"
    apart from "a tenant admin composed a two-string role five minutes ago and
    handed it to somebody with no seat anywhere" -- and those two must NOT
    answer the same way, or `DELEGATABLE_PERMISSIONS` quietly handed every
    tenant-defined role author company-wide read access the moment this design
    landed, which is exactly the "role assignment never widens `scope.
    viewable`" invariant this design's own text claims and the code did not, in
    fact, enforce.

    Bypass is admitted for the TOKEN floor (`source == "token"`, which is
    always one of the built-in role names in `permissions_for`) and for an
    ASSIGNED row that is ITSELF `builtin=True` (org_admin/dept_manager/
    operator/auditor explicitly assigned) -- both are the same "this person
    holds a company-wide role" fact `require_permission`-gated routes have
    always read this frozenset to mean. An assigned, tenant-defined
    (`builtin=False`) row never bypasses, regardless of what it holds: the
    caller's visibility comes from `DepartmentScope.viewable` (their seats)
    alone, exactly like every other delegatable `:view` permission already
    behaves (`approvals/repo.py::visible_approvals` skips its department filter
    only for `scope.is_unrestricted`, never for holding `approval:view` itself).
    """
    if permission not in authority.tenant_wide:
        return False
    if authority.source == "token":
        return True
    return authority.role is not None and authority.role.builtin


async def member_holds_assigned_permission(
    db: AsyncSession, *, member: OrgMember, permission: str
) -> bool:
    """Whether a channel-bound member (no token, no Principal -- see
    decision_from's own comment on why a ChannelActor holds no tenant-wide
    permission today) holds a given ASSIGNED-ROLE permission directly.

    Deliberately does not fall back to a token-derived default the way
    authz.authority._authority_of_member does for an authenticated operator:
    there is no token here to derive one from, so a member with no role_id
    gets nothing (fail closed) rather than inheriting some default role's
    grants.
    """
    if member.role_id is None:
        return False
    _role, granted = await _role_and_permissions(db, member.role_id)
    return permission in granted
