"""Who may do what, readable (§5.2, §17.2-6).

The permission layer decides correctly and says almost nothing. An operator who
is refused gets `403 requires permission: plugin:manage` and no way to find out
which role holds it, whether their own role is the one they think, or whether
their token carries a role the deployment recognises at all. That last case is
the one that hurts: an unrecognised role holds nothing, so the symptom is a
screen that is simply empty everywhere, which reads as a broken product rather
than as a mapping nobody finished.

So this returns the whole STATIC model rather than only the caller's slice:
every built-in role, every permission, and which of them the caller holds. None
of that is secret -- it is the same table a reader of `oc8.authz.permissions`
sees -- and hiding it would buy nothing while making every 403 undiagnosable.

It therefore carries NO permission of its own, which was not the first attempt:
gating it on `settings:view` locked out precisely the caller it exists for, since
an unrecognised role holds nothing at all. A test written to assert the intent
caught the implementation contradicting it. Authentication is still required --
the caller's own role is part of the answer -- but no grant is.

**And that reason has to stay literally true now that roles can be tenant-defined.**
So this endpoint answers about the CALLER and about the compiled-in model, and
about nobody else: which roles this tenant has invented, and who holds them, is
`GET /roles`, behind `role:view`. Otherwise this would be the one route on which
a seatless employee could read the whole company's authority map -- a better
reconnaissance target than most of what the layer protects -- and 12c40e6's
decision would have to be argued again with the opposite answer.

Two more things it now says, and one it survives:

* **The SOURCE of the caller's authority.** "Aus Ihrer Rolle `operator`" and
  "aus der Rolle *Freigabe Vertrieb*, die Ihnen zugewiesen wurde" are different
  sentences, and only one of them tells somebody who to go and ask.
  `callerPermissions` is the RESOLVED set rather than `permissions_for(token)`,
  which for exactly the population this slice creates would have rendered "you
  hold 52 of 52" to somebody refused at 48 of them.
* **Human and agent roles as two lists.** The backend sorts built-ins by name,
  so `agent_default` -- whose entire content is `tool:read|write|send` -- was the
  LEFTMOST column of the governance matrix, next to `plugin:manage`, as though a
  person could be given it.
* **A database that does not answer.** The read runs inside a SAVEPOINT and its
  failure degrades to `authorityUnavailable: true` with the static model intact.
  `get_db` yields inside `tenant_session`, which COMMITS on the way out, so a
  failed statement leaves the transaction aborted and a plain `try/except` in
  this handler would still 500 after it had returned its careful fallback. The
  screen that explains a refusal must never be the first thing to die.

Read-only, deliberately. The editor for tenant roles is `api/v1/roles.py`, gated
on `role:manage`; this stays the page that explains the model to whoever it just
refused, and gains no button that would need a permission to press.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError

from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.api.v1._serializers import seat_to_dto
from oc8.auth import Principal
from oc8.authz.authority import Authority, authority_for_principal
from oc8.authz.permissions import (
    ALL_PERMISSIONS,
    BUILTIN_ROLE_PERMISSIONS,
    SEAT_PERMISSIONS,
    permissions_for,
    role_kind,
)
from oc8.licensing import ALL_CAPABILITIES, get_entitlements_resolver
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import SeatDTO
from oc8.workspace.members import Seat, seats_for

logger = logging.getLogger(__name__)

router = APIRouter()

#: The two populations of the `role` table, kept apart on the wire as well as in
#: the column. Derived from `role_kind` rather than from a literal list, so the
#: screen and the PDP cannot disagree about which population a name belongs to.
HUMAN = "human"
AGENT = "agent"


class RoleDTO(CamelModel):
    name: str
    builtin: bool
    permissions: list[str]
    #: `human` | `agent`. On the wire because the two lists below are the same
    #: shape and a client that merged them would silently recreate the conflation
    #: this slice removed.
    kind: str = HUMAN
    #: ALWAYS NULL on this endpoint, and that is the shape rather than an
    #: oversight: everything listed here is compiled in, so there is no row to
    #: address, and looking one up would be a tenant read on the one route that
    #: promises to perform none. It is on the wire because `GET /roles` returns
    #: the same rows WITH ids and a client rendering both should not need two
    #: shapes -- and because a client that finds an id here would have found it
    #: by inventing one.
    id: str | None = None


class GovernanceDTO(CamelModel):
    #: Every permission the system knows, sorted. The screen groups by the part
    #: before the colon, so this needs no second, drifting list of resources.
    permissions: list[str]
    #: The built-in roles a PERSON can be, read-only because they are code.
    roles: list[RoleDTO]
    #: The built-in roles an AGENT can be. A separate list, not a flag on the one
    #: above: they describe what a program may do in a customer's system,
    #: intersected with its department frame and its own narrowing, and they can
    #: only ever subtract. Nobody can be given one.
    agentRoles: list[RoleDTO]
    #: The caller's own role name exactly as their token carries it -- NOT
    #: normalised to a known one. An unrecognised role must be visible as itself,
    #: because "your token says `menber`" is the answer, and "unknown" is not.
    callerRole: str
    #: What the caller ACTUALLY holds, resolved: the token's role, or the role an
    #: administrator assigned them. Never `permissions_for(token.role)` on its
    #: own -- a demoted administrator's own diagnostic screen would otherwise
    #: tell him he holds all 52 while every gate refuses him.
    callerPermissions: list[str]
    #: The capabilities the caller's organisation holds, when the deployment
    #: resolves entitlements per organisation; null when it does not (unknown).
    #: A resolver that fails yields an empty list.
    callerCapabilities: list[str] | None = None
    #: `token` when nobody has assigned them anything (which is every caller on
    #: every tenant that has configured nothing), `assigned` when a row decides.
    callerRoleSource: str
    #: The name of the assigned role, or null when the token decides. This is the
    #: word an employee repeats to their administrator.
    callerTenantRoleName: str | None
    #: `human` for a person. On the wire so the screen can state it rather than
    #: assume it, and so an assignment that somehow points at an agent-kind row
    #: is visible as itself instead of as "this role grants nothing".
    callerRoleKind: str
    #: False when the caller's role is not one the deployment defines. The screen
    #: leads with this: it is the difference between "you may not" and "nobody
    #: mapped your group yet".
    callerRoleIsKnown: bool
    #: What a seat can carry, so the screen can say what a departmental grant
    #: means without hardcoding the two words. Closed at four permissions on
    #: purpose (§0.A) -- a fifth is not a config change.
    seatRoles: dict[str, list[str]]
    #: The caller's OWN live seats. Empty is the answer to "why does my workspace
    #: refuse me": their token holds nothing tenant-wide and nobody has put them
    #: in a department, and those two together are the sentence a 403 does not
    #: give. Deliberately not folded into `callerPermissions`, which stays exactly
    #: what the ROLE grants -- a seat is authority somewhere, not everywhere, and
    #: a flat list cannot say where.
    seats: list[SeatDTO]
    #: True when the database could not be read. The static model is still
    #: returned; the caller's own half is missing and says so, rather than the
    #: page 500ing. A screen that explains refusals is the worst page in the
    #: product to lose during an incident.
    authorityUnavailable: bool = False
    #: True when a role WAS assigned to this caller and that row cannot grant --
    #: it is soft-deleted, or it is an agent-kind row, or the resolver refused it
    #: for a reason it logged. The person holds nothing tenant-wide, and the
    #: reason is a broken assignment rather than anything about their token.
    #:
    #: It exists because without it this screen was confidently wrong in the one
    #: state that is a genuine misconfiguration. `authority.role` is deliberately
    #: NULL for an unusable assignment (a name printed beside an empty set reads
    #: as "the role is empty" rather than "the role is unusable"), so
    #: `callerTenantRoleName` is null, so the screen fell through to *"your token
    #: carries `org_admin`, which holds nothing company-wide BY DESIGN"* -- both
    #: halves false, and it sends the administrator to look at seats when the fix
    #: is one nullable column. The two facts that say so are already resolved;
    #: this is the pair, named, so the screen can render the right sentence.
    callerRoleUnusable: bool = False


def _builtin_roles(kind: str) -> list[RoleDTO]:
    """One of the two populations, from CODE.

    `builtin=True` is a constant here and it is no longer the hardcoded value it
    used to be: since the tenant's own roles moved to `GET /roles`, everything
    this endpoint lists IS compiled in, and a row that is not built-in cannot
    reach this function. The flag stays on the wire because the screen renders
    "im Code definiert -- nicht änderbar" from it, and it must not have to infer
    that from the endpoint it happened to call.

    Split by `role_kind`, not by a literal list of names: the PDP decides which
    population a name belongs to with the same function, so the screen and the
    gate cannot come to disagree about `agent_default`.
    """
    return [
        RoleDTO(name=name, builtin=True, permissions=sorted(granted), kind=kind)
        for name, granted in sorted(BUILTIN_ROLE_PERMISSIONS.items())
        if role_kind(name) == kind
    ]


async def _caller_capabilities(principal: Principal) -> list[str] | None:
    resolver = get_entitlements_resolver()
    if resolver is None:
        return None
    try:
        entitlements = await resolver(principal)
        return sorted(c for c in ALL_CAPABILITIES if entitlements.has(c))
    except Exception:
        logger.warning("governance could not resolve the caller's capabilities", exc_info=True)
        return []


@router.get(
    "/governance",
    response_model=GovernanceDTO,
    dependencies=[
        Depends(
            unguarded(
                "explains the permission model to whoever it just refused, so a "
                "permission gate here would lock out exactly the caller who needs "
                "it; returns the static model plus the CALLER'S OWN role, grants "
                "and their source, and the caller's own seats -- it never "
                "enumerates this tenant's other roles or members, which is "
                "GET /roles behind role:view"
            )
        )
    ],
)
async def governance(request: Request, principal: CurrentPrincipal, db: DbSession) -> GovernanceDTO:
    authority: Authority | None = None
    seats: list[Seat] = []
    unavailable = False
    try:
        # SAVEPOINT, not a bare try/except. `get_db` yields inside
        # `tenant_session`, which commits at exit: a failed statement aborts the
        # whole Postgres transaction, so without this the careful fallback below
        # would be built, returned, and then destroyed by an exception on the way
        # out of the dependency. `begin_nested` gives the failure somewhere to be
        # rolled back to.
        async with db.begin_nested():
            # `upsert=False` is implied: this resolver never mints a row. A
            # caller the system has never seen gets no member and no seats, which
            # is the honest answer and is what makes a diagnostic read a read.
            authority = await authority_for_principal(request, db, principal)
            if authority.member is not None:
                # Authority, not the record: same reason as `GET /me`. A seat in
                # an archived department would explain the wrong refusal.
                seats = await seats_for(db, authority.member.id, include_archived=False)
    except SQLAlchemyError:
        logger.warning("governance could not resolve the caller's authority", exc_info=True)
        authority, seats, unavailable = None, [], True

    # When the read failed, this falls back to what the TOKEN says rather than to
    # nothing. It is the honest half of the answer -- the caller's own token is
    # readable without the database -- and `authorityUnavailable` beside it says
    # the other half is missing. Rendering an empty set instead would tell an
    # administrator he had been demoted during an incident he is already trying
    # to diagnose; every gate answers 503 in that state, not 403.
    caller_permissions = (
        permissions_for(principal.role) if authority is None else authority.tenant_wide
    )
    assigned = None if authority is None else authority.role
    # `source == "assigned"` is set only when `org_member.role_id` is NOT NULL,
    # and `role` is left NULL in that case only when `_role_and_permissions`
    # refused the row. So the pair is exact: an assignment exists and it cannot
    # grant. Derived from what is already resolved -- no second query, and no
    # change to the resolver's rule that an unusable role has no name.
    unusable = authority is not None and authority.source == "assigned" and assigned is None

    return GovernanceDTO(
        callerCapabilities=await _caller_capabilities(principal),
        permissions=sorted(ALL_PERMISSIONS),
        roles=_builtin_roles(HUMAN),
        agentRoles=_builtin_roles(AGENT),
        callerRole=principal.role,
        callerPermissions=sorted(caller_permissions),
        callerRoleSource="token" if authority is None else authority.source,
        callerTenantRoleName=None if assigned is None else assigned.name,
        callerRoleKind=HUMAN if assigned is None else assigned.kind,
        callerRoleIsKnown=principal.role in BUILTIN_ROLE_PERMISSIONS,
        seatRoles={name: sorted(granted) for name, granted in sorted(SEAT_PERMISSIONS.items())},
        seats=[seat_to_dto(s) for s in seats],
        authorityUnavailable=unavailable,
        callerRoleUnusable=unusable,
    )
