"""Composing a role, and handing it to somebody (§6).

**The only module that writes `role` or `role_permission`.** Every refusal in
this file exists because the alternative is an administrator being told
something false: a permission silently dropped, a name that will never resolve,
a delete that demotes forty people, or a caller handing out authority he does
not hold himself.

**Nothing here commits.** Services flush; the caller commits. That is the house
rule, and on this path it is load-bearing rather than stylistic: `PUT /roles/{id}`
DELETEs the old grant set and INSERTs the new one, and a commit between the two
would unbind `app.tenant_id` for the rest of the transaction -- the INSERTs would
then match no RLS policy and affect zero rows, leaving the role holding nothing
and the response saying it worked.

**Two refusals are not what they look like.**

* The BUILT-IN NAME check is not the unique index restated. Globex has exactly
  one `role` row, so on that tenant `operator` is a free name -- the index would
  admit it, and the row would then be ignored by the resolver for ever, because
  `builtin` is false and the code table is never consulted for a tenant row. An
  administrator would have composed a role called `operator` that grants what he
  ticked, while every screen in the product says `operator` means something else.
* The DELEGATABLE check runs here AND in the resolver, and the duplication is
  deliberate. Here it names the offender to the person who ticked it; there it
  makes a row that arrived by restore or by psql inert. Neither is redundant:
  drop the first and an admin is told nothing, drop the second and a row nobody
  validated decides.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from oc8.auth.two_factor_policy import enrolled_methods

# `HUMAN` from the resolver, not a second literal: the string this module WRITES
# has to be the string the resolver ACCEPTS, and two spellings of "human" that
# drift apart would produce roles that save cleanly and grant nothing.
from oc8.authz.authority import HUMAN as HUMAN_KIND
from oc8.authz.permissions import (
    AGENT_DEFAULT,
    ALL_PERMISSIONS,
    BUILTIN_ROLE_PERMISSIONS,
    DELEGATABLE_PERMISSIONS,
    MANAGE,
    MEMBER,
    delegation_refusal,
    perm,
    permissions_for,
    role_kind,
)
from oc8.models.core import Role, RolePermission
from oc8.models.identity import OrgMember

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

MEMBER_MANAGE: Final = perm(MEMBER, MANAGE)

#: A role name is a caption on a screen and a word in an audit trail, so it is
#: bounded and printable, and that is all.
#:
#: The design writes it as `^[a-z0-9][a-z0-9 _-]{1,62}$`, which cannot be the
#: rule: this product's screens are German and its first real role is called
#: *Freigabe Vertrieb*. Lower-case-ASCII-only would refuse every name an
#: administrator would actually type, so the class is "a letter or a digit, in
#: any language" and the separators stay the same three.
#:
#: What it still refuses is what matters: a colon (so a name can never be
#: mistaken for a permission string in a message or a log line), leading
#: punctuation, control characters, and anything long enough to break the
#: layout it will be rendered in. Length 2..63.
_NAME = re.compile(r"[^\W_][\w \-]{1,62}", re.UNICODE)

#: Names a tenant may not take, whatever the `role` table happens to contain.
#: `agent_default` is in here twice over -- it is a built-in AND it is the name
#: that, before `role.kind` existed, handed an agent every tool right there is.
_RESERVED: Final[frozenset[str]] = frozenset(
    {name.lower() for name in BUILTIN_ROLE_PERMISSIONS} | {AGENT_DEFAULT}
)


class RoleRefused(Exception):
    """A refusal with the status code the caller should see, and a sentence.

    One exception rather than five, carrying its own status: the router maps it
    straight to an `HTTPException`, so the reason a request was refused is
    written next to the rule that refused it instead of being reconstructed from
    an exception type two files away.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class RoleSummary:
    """A role as the list screen shows it.

    `holder_count` is the BLAST RADIUS, and it is on the list rather than only on
    the detail because the sentence this whole feature exists for -- "jede
    Teamleitung darf ab jetzt auch das Prüfprotokoll lesen" -- is one edit that
    changes forty people's authority. An administrator who cannot see the forty
    before he clicks Save is being asked to guess.
    """

    id: uuid.UUID | None
    name: str
    description: str
    kind: str
    builtin: bool
    permissions: frozenset[str]
    holder_count: int

    @property
    def can_edit(self) -> bool:
        """Built-ins are read-only, and not out of caution.

        Their grants come from `BUILTIN_ROLE_PERMISSIONS` -- the resolver reads
        the code table for any row with `builtin=true` -- so a `role_permission`
        row written against one is inert. An editor over them would save
        successfully, show the new set, and change nothing at any gate.
        """
        return not self.builtin


# ------------------------------------------------------------------- validation


def validated_name(name: str) -> str:
    """The name as it will be stored, or a refusal naming the rule.

    Stripped BEFORE both checks, because `" agent_default "` is the same name to
    a person and a different one to `lower(name)`. The unique index would let
    that through and the resolver would then treat it as an ordinary tenant role
    -- which is the trap this slice exists to close, arrived at by two spaces.
    """
    cleaned = " ".join(name.split())
    if not _NAME.fullmatch(cleaned):
        raise RoleRefused(
            422,
            f"invalid role name {name!r}: 2 to 63 characters, starting with a "
            "letter or a digit, and only letters, digits, spaces, hyphens and "
            "underscores after it",
        )
    if cleaned.lower() in _RESERVED:
        raise RoleRefused(
            409,
            f"{cleaned!r} is a built-in role name. A tenant role by that name "
            "would never resolve -- built-in roles are read from code -- and "
            "every screen would say it means something else.",
        )
    return cleaned


def validated_permissions(permissions: list[str]) -> frozenset[str]:
    """The requested set, or a 422 naming the first offender and WHY.

    Never a silent drop. An administrator who ticks `plugin:manage`, is answered
    200, and walks away believing the team lead can install plugins has been told
    something false by the product -- and the person he told it to will find out
    at the worst possible moment.

    The reason string is `delegation_refusal`'s, i.e. the same sentence the
    catalogue renders next to the disabled checkbox. One string, two readers, so
    the form and the error cannot disagree.
    """
    requested = list(dict.fromkeys(permissions))
    unknown = [p for p in requested if p not in ALL_PERMISSIONS]
    if unknown:
        raise RoleRefused(
            422,
            f"unknown permission(s): {', '.join(sorted(unknown))}. "
            "See GET /permissions/catalogue for the ones that exist.",
        )
    refused = [p for p in requested if p not in DELEGATABLE_PERMISSIONS]
    if refused:
        first = sorted(refused)[0]
        reason = delegation_refusal(first) or "not offerable in a tenant-defined role"
        raise RoleRefused(
            422,
            f"{first} may not be put in a tenant-defined role: {reason}. "
            f"Refused: {', '.join(sorted(refused))}.",
        )
    return frozenset(requested)


def bounded_by_the_caller(
    caller: frozenset[str], *, before: frozenset[str], after: frozenset[str], what: str
) -> None:
    """`(before | after) ⊆ caller.tenant_wide`, or 403 naming the excess.

    VACUOUS TODAY, and implemented anyway. `role:manage` and `member:manage` are
    both `NEVER_DELEGATABLE`, so the only caller who reaches any of these routes
    is `org_admin`, who holds all 52 -- every comparison below is arithmetic on a
    superset. The day either permission graduates, nobody will remember that this
    rule was the thing standing between "an administrator may compose roles" and
    "an administrator may compose a role stronger than himself", and
    `test_the_subset_rule_is_vacuous_today_and_will_not_stay_that_way` is what
    tells whoever graduates it that the tests for this are still owed.

    `before` as well as `after` is not symmetry for its own sake: without the
    BEFORE term, a caller who may not GRANT `audit:view` could still REVOKE it
    from a role that has it, which is authority over somebody else's authority
    by a different verb. And applying the rule to `?reassignTo=`'s target is what
    stops `DELETE /roles/{weak}?reassignTo={strong}` being a one-call bypass in
    which the caller never names a permission at all.
    """
    excess = (before | after) - caller
    if excess:
        raise RoleRefused(
            403,
            f"you may not {what} a role holding permissions you do not hold "
            f"yourself: {', '.join(sorted(excess))}",
        )


# ------------------------------------------------------------------------ reads


async def load_role(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    for_update: bool = False,
) -> Role | None:
    """One live role of this tenant, or None.

    The tenant predicate is written out although RLS applies it: this row decides
    what a person may do, and a read that would cross tenants on an unbound
    session is not a read to leave implicit. `deleted_at` likewise --
    `SoftDeleteMixin` adds no filter, so a deleted role is otherwise perfectly
    editable and perfectly assignable.

    **`for_update=True` is what makes a revocation a revocation.** `update_role`
    is read-then-DELETE-then-INSERT, and under READ COMMITTED two of those
    interleave into a result neither administrator asked for. Reproduced against
    Postgres 15 before this argument existed, with the real
    `uq_role_permission (role_id, permission)` index:

    * *"take everything away"* concurrent with *"add `audit:view`"* answered 200
      to both and revoked nothing -- the second transaction's DELETE blocked on
      the first's row locks, re-evaluated, matched zero rows, and then inserted
      alongside grants its own snapshot could not see;
    * two disjoint edits merged into their UNION, which is the exact failure
      `UpdateRoleRequest`'s docstring justifies the replacement shape by
      preventing;
    * two overlapping edits -- one administrator double-clicking Save is enough
      -- raised a bare `UniqueViolation` out of the INSERT and answered 500.

    And in the first two the audit event states a diff that did not happen,
    because `before` was read before the lock existed.

    `populate_existing=True` because the role may already be in this session's
    identity map: an administrator editing a role he holds himself has had it
    loaded by the resolver, and a locking SELECT that returned the cached
    attributes would take the lock and then compute the diff from the stale copy,
    which is the defect wearing a lock.

    **It locks ONE row, and only the row being written.** `?reassignTo=`'s target
    is read, never written, so it is deliberately NOT locked: two administrators
    each deleting a role while naming the other's as the target would otherwise
    take the two row locks in opposite orders, and a deadlock is a worse defect
    than the stale read it would be closing.

    **`FOR NO KEY UPDATE`, not `FOR UPDATE`, and the difference is a deadlock.**
    `role_permission` and `org_member` both carry a composite foreign key into
    `role`, so every INSERT of a grant and every assignment of a person takes an
    implicit `FOR KEY SHARE` on the role row. `FOR UPDATE` conflicts with that;
    `FOR NO KEY UPDATE` does not, and it still conflicts with itself, which is the
    only exclusion this needs. With `FOR UPDATE` the deletion path acquires the
    two in opposite orders and Postgres aborts one of them: two administrators
    each deleting a role while naming the other's as `?reassignTo=` would hold a
    lock on their own role and wait for a KEY SHARE on the other's. That is the
    worse defect this whole argument is about, reached by closing the smaller one
    with the bigger lock.
    """
    stmt = select(Role).where(
        Role.tenant_id == tenant_id,
        Role.id == role_id,
        Role.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update(key_share=True).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalars().first()


async def grants_of(db: AsyncSession, role: Role) -> frozenset[str]:
    """What this role grants, from the right source for its kind.

    Built-in rows resolve from CODE, exactly as `authz.authority` resolves them,
    so this and the gate cannot disagree about what `operator` means. Tenant rows
    resolve from their `role_permission` rows -- raw, without the delegatable
    intersection, because this answer is also what the EDITOR renders and a
    checkbox that silently unticks itself on load is worse than one that shows a
    row the resolver ignores.
    """
    if role.builtin:
        return permissions_for(role.name)
    rows = (
        await db.execute(
            select(RolePermission.permission).where(
                RolePermission.tenant_id == role.tenant_id,
                RolePermission.role_id == role.id,
            )
        )
    ).scalars()
    return frozenset(rows)


async def grants_for_many(
    db: AsyncSession, tenant_id: uuid.UUID, role_ids: list[uuid.UUID]
) -> dict[uuid.UUID, frozenset[str]]:
    """Every role's grants in ONE query, for the list screen.

    One query and not one per role, for the same reason `seats_for_many` exists:
    the obvious shape -- resolve each role's grants as you render it -- turns a
    tenant with twenty roles into twenty-one round trips on a screen nobody
    thinks of as expensive.
    """
    if not role_ids:
        return {}
    rows = (
        await db.execute(
            select(RolePermission.role_id, RolePermission.permission).where(
                RolePermission.tenant_id == tenant_id,
                RolePermission.role_id.in_(role_ids),
            )
        )
    ).all()
    out: dict[uuid.UUID, set[str]] = {}
    for role_id, permission in rows:
        out.setdefault(role_id, set()).add(permission)
    return {k: frozenset(v) for k, v in out.items()}


async def holders_of(
    db: AsyncSession, *, tenant_id: uuid.UUID, role_id: uuid.UUID, limit: int | None = None
) -> list[OrgMember]:
    """Everybody currently pointed at this role.

    The list a `409` prints when a delete is refused. "This role is in use" with
    no names is a refusal an administrator cannot act on: he has to go and find
    the holders before he can decide whether to move them or leave the role
    alone, and the endpoint already knows.

    `limit=None` is UNBOUNDED and is what `delete_role` asks for -- it moves every
    holder and must not leave the four hundred and first behind. Everything that
    merely SHOWS holders passes a limit and reads the count separately, because
    "how many people does this change" has to be exact while "which of them" is a
    list somebody reads.
    """
    stmt = (
        select(OrgMember)
        .where(
            OrgMember.tenant_id == tenant_id,
            OrgMember.role_id == role_id,
            OrgMember.deleted_at.is_(None),
        )
        .order_by(OrgMember.display_name, OrgMember.subject)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def holder_count_of(db: AsyncSession, *, tenant_id: uuid.UUID, role_id: uuid.UUID) -> int:
    """How many people hold this role. Exact, and independent of any preview."""
    return int(
        (
            await db.execute(
                select(func.count()).where(
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.role_id == role_id,
                    OrgMember.deleted_at.is_(None),
                )
            )
        ).scalar_one()
    )


async def _holder_counts(db: AsyncSession, tenant_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = (
        await db.execute(
            select(OrgMember.role_id, func.count())
            .where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.role_id.is_not(None),
                OrgMember.deleted_at.is_(None),
            )
            .group_by(OrgMember.role_id)
        )
    ).all()
    return {role_id: count for role_id, count in rows if role_id is not None}


async def list_roles(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[RoleSummary]:
    """The built-in ladder, then this tenant's own roles.

    The built-ins come from CODE and not from the `role` table, and that is not a
    shortcut: `provision.py` writes five rows per tenant but Globex has one, and
    nothing has ever backfilled the difference. A list built from rows would show
    a different, shorter ladder on the older tenant -- for a set of roles that is
    identical in both, because it is compiled in. Where a row DOES exist its id
    is carried through, so the screen can address it and `holderCount` counts the
    people pointed at it.

    Sorted by how much each grants, descending, which is the ladder as the design
    describes it (52 / 29 / 20 / 19 / 0) rather than the alphabet -- `auditor`
    first would read as a hierarchy that is not one. Tenant roles follow, by name.
    """
    rows = list(
        (
            await db.execute(
                select(Role)
                .where(Role.tenant_id == tenant_id, Role.deleted_at.is_(None))
                .order_by(Role.name)
            )
        )
        .scalars()
        .all()
    )
    counts = await _holder_counts(db, tenant_id)
    tenant_rows = [r for r in rows if not r.builtin and r.kind == HUMAN_KIND]
    grants = await grants_for_many(db, tenant_id, [r.id for r in tenant_rows])
    by_name = {r.name.lower(): r for r in rows if r.builtin}

    builtin: list[RoleSummary] = []
    for name, granted in BUILTIN_ROLE_PERMISSIONS.items():
        # `role_kind`, not `name != AGENT_DEFAULT`: which population a name
        # belongs to is decided in ONE expression that the PDP reads too, so this
        # list and the agent's resolver cannot come to disagree. The agent's own
        # role is not offerable to a person -- assigning it is refused -- and
        # listing it here would put `tool:send` in a picker of human roles.
        if role_kind(name) != HUMAN_KIND:
            continue
        row = by_name.get(name.lower())
        builtin.append(
            RoleSummary(
                id=None if row is None else row.id,
                name=name,
                description="" if row is None else row.description,
                kind=HUMAN_KIND,
                builtin=True,
                permissions=granted,
                holder_count=0 if row is None else counts.get(row.id, 0),
            )
        )
    builtin.sort(key=lambda s: (-len(s.permissions), s.name))

    own = [
        RoleSummary(
            id=r.id,
            name=r.name,
            description=r.description,
            kind=r.kind,
            builtin=False,
            permissions=grants.get(r.id, frozenset()),
            holder_count=counts.get(r.id, 0),
        )
        for r in tenant_rows
    ]
    return builtin + own


# ----------------------------------------------------------------------- writes


async def _name_is_taken(db: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> bool:
    """Including soft-deleted rows, because the index includes them.

    `uq_role_tenant_name` is unconditional on purpose: renaming is refused, so
    delete-and-recreate is the sanctioned way to change a name, and a partial
    index would free the old name the moment the old role was deleted -- handing
    every mention of it in the audit trail to a role that is not the one that
    earned them. Checked here as well so the answer is a sentence rather than a
    `duplicate key value violates unique constraint` the caller has to decode.
    """
    return (
        await db.execute(
            select(Role.id).where(
                Role.tenant_id == tenant_id,
                func.lower(Role.name) == name.lower(),
            )
        )
    ).first() is not None


async def create_role(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    description: str,
    permissions: frozenset[str],
    created_by: uuid.UUID | None,
) -> Role:
    """Write the role and its grants. Flushed, never committed.

    `kind` is written as `'human'` unconditionally and there is no parameter for
    it. Tenant-defined AGENT roles are deferred, not forgotten -- "this agent may
    only read" is real and is a second editor gated on `agent:manage` -- and
    until then there must be no path from this form into the agent's population,
    including by adding a key to the request body.
    """
    if await _name_is_taken(db, tenant_id=tenant_id, name=name):
        raise RoleRefused(
            409,
            f"a role named {name!r} already exists in this tenant (names stay "
            "taken after a delete, so that an old name never comes back meaning "
            "something new)",
        )
    role = Role(
        tenant_id=tenant_id,
        name=name,
        builtin=False,
        kind=HUMAN_KIND,
        description=description,
        created_by=created_by,
    )
    db.add(role)
    try:
        await db.flush()
    except IntegrityError as exc:  # pragma: no cover - the pre-check catches this first
        raise RoleRefused(409, f"a role named {name!r} already exists in this tenant") from exc
    for permission in sorted(permissions):
        db.add(RolePermission(tenant_id=tenant_id, role_id=role.id, permission=permission))
    await db.flush()
    return role


async def update_role(
    db: AsyncSession, *, role: Role, description: str, permissions: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Full replacement of the grant set. Returns `(added, removed)` for the audit.

    DELETE-then-INSERT in ONE transaction, committed once by the caller. A commit
    between the two halves would unbind `app.tenant_id` and the INSERTs would
    match no policy: the role would come out holding nothing, the response would
    say it worked, and forty people would quietly stop being able to do their
    jobs.

    The diff is returned rather than logged here because it is what the audit
    event carries: "role.updated" with no added/removed is an entry that records
    that something changed and refuses to say what.
    """
    if role.builtin:
        raise RoleRefused(
            409,
            f"{role.name!r} is a built-in role: its grants come from code, so "
            "rows written against it would be ignored by every gate",
        )
    before = await grants_of(db, role)
    await db.execute(
        delete(RolePermission).where(
            RolePermission.tenant_id == role.tenant_id,
            RolePermission.role_id == role.id,
        )
    )
    for permission in sorted(permissions):
        db.add(RolePermission(tenant_id=role.tenant_id, role_id=role.id, permission=permission))
    role.description = description
    await db.flush()
    return permissions - before, before - permissions


async def delete_role(
    db: AsyncSession,
    *,
    role: Role,
    reassign_to: Role | None,
    caller_subject: str,
) -> list[OrgMember]:
    """Soft-delete a role, moving its holders first if a target was named.

    Returns the holders that were moved. Refuses -- rather than demoting anybody
    -- when the role is held and no target was given: `ON DELETE RESTRICT` is
    what makes that a refusal at the database as well, and a role deleted out
    from under forty people would silently restore every one of them to whatever
    their TOKEN says, which on every live tenant is `org_admin`.

    The name is NOT freed. `uq_role_tenant_name` is unconditional, so the row
    keeps its name for ever -- the audit trail's mentions of it stay unambiguous,
    and a new role cannot inherit them by taking the word back.

    `?reassignTo=` MOVES people, so it is an assignment and carries the same
    self-demotion refusal `assign_role` carries. A caller who holds the role he is
    deleting is one of the people being moved, and moving himself to a role
    without `member:manage` locks the only door back just as surely as assigning
    it to himself by name would -- the check lives in both places because the two
    routes are two verbs over one act, and `assign_role` is not on this path.
    Vacuous today for the same reason `bounded_by_the_caller` is; implemented for
    the same reason.
    """
    if role.builtin:
        raise RoleRefused(409, f"{role.name!r} is a built-in role and cannot be deleted")
    holders = await holders_of(db, tenant_id=role.tenant_id, role_id=role.id)
    if holders and reassign_to is None:
        named = ", ".join(h.display_name or h.subject for h in holders[:10])
        more = "" if len(holders) <= 10 else f" and {len(holders) - 10} more"
        raise RoleRefused(
            409,
            f"{len(holders)} person(s) hold {role.name!r}: {named}{more}. "
            "Delete it with ?reassignTo=<roleId> to move them, or take it away "
            "from them first -- deleting it silently would restore each of them "
            "to whatever their token says.",
        )
    if reassign_to is not None:
        if reassign_to.id == role.id:
            raise RoleRefused(422, "?reassignTo= names the role being deleted")
        if reassign_to.kind != HUMAN_KIND:
            raise RoleRefused(422, "?reassignTo= names a role that is not a human role")
        if any(holder.subject == caller_subject for holder in holders):
            target_grants = await grants_of(db, reassign_to)
            if MEMBER_MANAGE not in target_grants:
                raise RoleRefused(
                    409,
                    f"you hold {role.name!r}, so deleting it with "
                    f"?reassignTo={reassign_to.name!r} would move YOU to a role "
                    f"without {MEMBER_MANAGE} -- the permission that assigns "
                    "roles. Take the role away from yourself first, or name a "
                    "target that keeps you able to assign.",
                )
        for holder in holders:
            holder.role_id = reassign_to.id
    role.deleted_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()
    return holders


async def assign_role(
    db: AsyncSession,
    *,
    member: OrgMember,
    role: Role | None,
    caller_subject: str,
    role_grants: frozenset[str] | None = None,
) -> None:
    """Point one person at one role, or back at the token floor.

    `role=None` is NOT "no permissions": it clears the override, and the person
    resolves through `permissions_for(token.role)` exactly as they did before
    this feature existed. That is why the column is nullable rather than
    defaulted, and why clearing it is the documented repair for a lockout.

    Two refusals:

    * an AGENT-kind role, because a person pointed at one resolves to the empty
      set -- a total, silent lockout that looks on every screen like a role that
      simply grants nothing;
    * SELF-DEMOTION to a role without `member:manage`, which is the assignment
      that locks the only door back. `member:manage` is `NEVER_DELEGATABLE`, so
      that is every tenant-defined role there can be -- the check is written
      against the permission rather than against "is it a tenant role", because
      the day one graduates, the rule that matters is still the permission.
      `oc8 member set-role --clear` is the out-of-band repair.
    """
    if role is not None and role.kind != HUMAN_KIND:
        raise RoleRefused(
            422,
            f"{role.name!r} is not a human role; a person pointed at it would hold nothing at all",
        )
    if role is not None and member.subject == caller_subject:
        # `role_grants` is the caller's memo for one request: a bulk assignment
        # asks this about ONE role for forty people, and the answer does not
        # change between them.
        granted = role_grants if role_grants is not None else await grants_of(db, role)
        if MEMBER_MANAGE not in granted:
            raise RoleRefused(
                409,
                f"assigning yourself {role.name!r} would take away {MEMBER_MANAGE}, "
                "which is the permission that assigns roles -- you would be "
                "locking the only door back. Have somebody else do it, or repair "
                "it out of band with `oc8 member set-role --clear`.",
            )
    member.role_id = None if role is None else role.id

    # Standalone 2FA design: org_admin is the role the default policy makes
    # mandatory. Start the grace clock on a grant that results in org_admin
    # with no credential yet; never restart an already-running clock (a
    # re-save of the same assignment must not reset an admin's countdown to
    # full); clear it on a demotion away from org_admin for someone who
    # never enrolled (nothing to clear for someone who did -- their clock is
    # already None).
    becomes_org_admin = role is not None and role.name == "org_admin"
    if becomes_org_admin:
        has_credential = bool(await enrolled_methods(db, member.id))
        if not has_credential and member.totp_grace_started_at is None:
            member.totp_grace_started_at = dt.datetime.now(tz=dt.UTC)
    elif member.totp_grace_started_at is not None:
        member.totp_grace_started_at = None

    await db.flush()
