"""Shared FastAPI dependencies: authentication and RLS-bound DB sessions."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.auth import Principal, get_identity_provider
from oc8.auth.provider import InvalidToken
from oc8.authz.authority import Authority, authority_for_principal
from oc8.authz.permissions import AGENT, ALL_PERMISSIONS, MANAGE, perm
from oc8.authz.scope import HumanActor, scope_for_principal
from oc8.db.session import tenant_session

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=True)


async def get_principal(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> Principal:
    try:
        return get_identity_provider().verify(creds.credentials)
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_db(
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[AsyncSession]:
    async with tenant_session(principal.tenant_id) as session:
        yield session


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _bearer_token(request: Request) -> str | None:
    """Pull the bearer token off the raw header, parsed EXACTLY the way
    `HTTPBearer` parses it -- via FastAPI's own `get_authorization_scheme_param`,
    which strips whitespace around the token.

    Shared by every router-level guard in this module that has to read the
    header directly (before FastAPI has resolved a route and before
    `HTTPBearer` has run). A hand-rolled `header.partition(" ")` does NOT
    strip that whitespace: `Authorization: Bearer  <token>` (two spaces)
    would hand a hand-rolled parser a leading-space-corrupted token, which
    fails `IdentityProvider.verify()` -- and a guard that treats a failed
    `verify()` as "not a token I recognize, let it through" then FAILS OPEN,
    while the identical request, parsed correctly by `HTTPBearer` a few lines
    later in the real dependency chain, verifies fine and comes out fully
    authenticated. Two spaces must never be a bypass.
    """
    scheme, token = get_authorization_scheme_param(request.headers.get("authorization"))
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def deny_agent_principals(request: Request) -> None:
    """Refuse a `kind=agent` token on the operator API.

    An agent token is minted for a container and travels INTO it, so it must
    reach exactly three surfaces: the LLM gateway, the tool gateway, and the
    internal agent API the reference shell drives. Each of those checks the
    token's `run:<id>` scope itself. NOTHING else did -- every other route
    treated an agent token as an ordinary tenant principal.

    That was reachable, not theoretical: the token handed to a nanoclaw container
    approved that container's OWN pending >3000 EUR approval with 200 OK, and the
    audit event recorded it as an operator's decision (verified against the
    running stack 2026-07-27). The container has a shell and can reach the
    backend, so a prompt injection in a CRM record was enough to lift the gate the
    whole approval design exists to hold.

    Applied at the router, not per endpoint: a hole that has to be remembered on
    every new route is a hole that comes back.

    Reads the header itself rather than depending on `CurrentPrincipal`, because
    a dependency that RESOLVES a principal also REQUIRES one -- and several routes
    under this prefix are deliberately unauthenticated (`/auth/config`, the signed
    GitHub webhook, the OAuth callback a browser is redirected to). This only ever
    subtracts: no token and an unreadable token both fall through untouched, to be
    handled by whatever the route already does.
    """
    token = _bearer_token(request)
    if token is None:
        return
    try:
        principal = get_identity_provider().verify(token)
    except InvalidToken:
        return
    if principal.kind == "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="an agent token may not use the operator API",
        )


#: Paths a `totp:enroll`-scoped token IS allowed to reach. Exact-string set
#: (NOT a path-prefix match): this dependency runs before FastAPI has
#: resolved which route matched, the same constraint `deny_agent_principals`
#: is already written against.
_TOTP_ENROLL_ALLOWED_PATHS = {
    "/api/v1/auth/totp/enroll",
    "/api/v1/auth/totp/confirm",
}

#: Paths a `totp:challenge`-scoped token IS allowed to reach. Deliberately a
#: SEPARATE, narrower set from `_TOTP_ENROLL_ALLOWED_PATHS`: a challenge token
#: proves a password only -- no second factor presented yet -- so it must be
#: confined to the ONE route that completes the challenge. It must NOT be
#: able to reach `/enroll`/`/confirm`: a password-only attacker holding a
#: victim's challenge token could otherwise try to register their OWN
#: authenticator on the victim's account. (A DB `UNIQUE` constraint on
#: `member_id` happens to turn that attempt into a 500 today rather than a
#: silent takeover, but that is incidental, not the intended defense --
#: this allowlist is.)
_TOTP_CHALLENGE_ALLOWED_PATHS = {
    "/api/v1/auth/totp/verify",
}


async def deny_totp_pending_principals(request: Request) -> None:
    """Refuse a narrow enrollment/challenge token everywhere except its own
    TOTP routes (standalone 2FA design).

    `POST /auth/login`'s outcome 2/3 (grace expired, or a challenge is
    required) mints a token carrying `scopes=["totp:enroll"]` or
    `["totp:challenge"]` instead of a real session -- deliberately still a
    normal, verifiable bearer token (so it round-trips through
    `IdentityProvider.mint()`/`verify()` unchanged), but one that must be
    structurally incapable of reaching anything but the routes that complete
    ITS OWN step: an enrollment token may only reach `/enroll` and `/confirm`,
    a challenge token may only reach `/verify` -- never the other's routes,
    because an enrollment token that could also reach `/verify` (or vice
    versa) would let a caller who has only proven ONE of "knows the password"
    / "has a TOTP secret to enroll" pass itself off as the other. Mirrors
    `deny_agent_principals` exactly: reads the bearer token directly (this
    dependency runs BEFORE FastAPI has resolved a route, so it cannot depend
    on `CurrentPrincipal`, which both resolves AND requires a principal),
    applied once at the router mount rather than per-endpoint so a new route
    can't forget it.
    """
    token = _bearer_token(request)
    if token is None:
        return
    try:
        principal = get_identity_provider().verify(token)
    except InvalidToken:
        return
    path = request.url.path
    if "totp:enroll" in principal.scopes:
        if path in _TOTP_ENROLL_ALLOWED_PATHS:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this token only completes TOTP enrollment",
        )
    if "totp:challenge" in principal.scopes:
        if path in _TOTP_CHALLENGE_ALLOWED_PATHS:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this token only completes a login challenge",
        )


def require_role(*roles: str) -> Callable[[Principal], Awaitable[Principal]]:
    """Deprecated: name a PERMISSION, not a role (see `require_permission`).

    Kept because it still guards routes that have not been converted, and
    removing it before they are would silently open them.
    """

    async def _dep(principal: CurrentPrincipal) -> Principal:
        if roles and principal.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires one of roles: {', '.join(roles)}",
            )
        return principal

    return _dep


async def _authority(request: Request, db: AsyncSession, principal: Principal) -> Authority:
    """The caller's resolved authority, or a 503.

    Not a 403. A gate that answers "you may not" when what actually happened is
    "the database did not answer" is the failure `authz/permissions.py`'s
    doctrine is written against: it locks every operator out during a blip, and
    it does it with the same status code a genuine refusal uses, so the screen
    tells them to ask an administrator for a permission they already hold.

    503 also stops this masking a schema fault quietly: a missing column would
    make EVERY gated route answer 503, which is loud, whereas 403 would read as
    "somebody changed the permissions" and be chased for a day.
    """
    try:
        return await authority_for_principal(request, db, principal)
    except SQLAlchemyError:
        logger.exception("could not resolve the caller's authority")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authorization is temporarily unavailable",
        ) from None


def require_permission(permission: str) -> Callable[..., Awaitable[Principal]]:
    """Gate a route on what the caller may DO, rather than on what it is called.

    Naming the permission instead of the roles is what makes a new role possible
    without editing every route, and what stops a route admitting a role by
    accident -- `require_role("org_admin", "member")` guarded one endpoint with a
    role name that is issued to nobody and is not in `BUILTIN_ROLES`, so an
    ordinary operator was refused there while the code read as if two roles were
    welcome. Nothing in a role list is checkable; a permission is.

    An unknown permission string raises at IMPORT time rather than at request
    time. A typo would otherwise produce a route that admits nobody -- including
    `org_admin`, who holds every permission that exists -- and that failure looks
    exactly like a deliberate lockout.

    **What the caller holds is now RESOLVED rather than read off the token.**
    `role_has(principal.role, permission)` answers "what does this role NAME
    grant", which is the wrong question the moment an administrator can assign a
    tenant-defined role: `org_member.role_id` is an override, and a gate that
    keeps asking the code table is a gate no demotion reaches. The two other
    things about the line are unchanged -- one permission, no resource, tenant-
    wide -- which is precisely why every `:manage` permission is refused to a
    tenant-defined role (`NEVER_DELEGATABLE`, decision C).

    `request` and `db` are the only additions to the signature, and they cost
    nothing measurable: all 110 permission-gated routes already resolve `get_db`,
    so this opens no session anywhere -- it runs on the connection and inside the
    transaction the route already has. `authority_for_principal` memoises on
    `request.state`, so a route carrying this gate and `require_departmental`
    pays for one lookup rather than two.

    **One statement, and two for a caller with an assignment.** That is the
    measured number rather than the hoped-for one:
    `test_the_gate_costs_one_statement_and_an_assignment_costs_two` counts EVERY
    statement the resolver issues, not the ones that mention a particular table.
    An earlier version of that test filtered on `"org_member"` and so reported
    one while three were running -- a three-table LEFT JOIN over the seat tables
    among them, filling a field no module read.

    `_dep.__qualname__` still contains `require_permission`, which is what
    `test_every_route_is_governed._guards` reads, and the closure still holds the
    permission string for `_closed_over_permission`.
    """
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}; add it to oc8.authz.permissions")

    async def _dep(request: Request, principal: CurrentPrincipal, db: DbSession) -> Principal:
        authority = await _authority(request, db, principal)
        if permission not in authority.tenant_wide:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires permission: {permission}",
            )
        return principal

    return _dep


def require_departmental(
    permission: str, *, or_tenant_wide: str | None = None
) -> Callable[..., Awaitable[HumanActor]]:
    """Admit a caller who holds `permission` tenant-wide OR in at least one seat
    OR (when given) `or_tenant_wide` tenant-wide.

    The workspace's door, and deliberately a SECOND gate rather than a widening
    of `require_permission`: only the routes that can be reached from a seat go
    looking for one, and the other 110 never load a seat they have no use for.

    `or_tenant_wide` exists for exactly one caller today: `chat.py`'s six
    session routes, which resolve a `HumanActor` (for its `member`/`scope`,
    used for session OWNERSHIP) via `require_departmental(perm(AGENT, VIEW))`
    even for a caller who holds no `agent:view` seat anywhere, as long as they
    hold `copilot:use` tenant-wide -- see `assistant_visible` in `oc8/chat/reach.py`. It is boxed in
    a 1-tuple and read back through a local inside `_dep` rather than closed
    over directly: `test_every_route_is_governed._closed_over_permission`
    returns the FIRST string-valued freevar cell it finds on the closure, and
    Python does not guarantee freevar cell order matches declaration order --
    an unboxed second string parameter could nondeterministically be picked up
    instead of `permission`, silently mislabelling which permission a route is
    pinned on.

    Both gates now read the same `Authority`, once per request, memoised -- so
    the difference between them is what they do with it and not where they
    read it from.

    What it yields is the resolved `HumanActor`, and what it does NOT do is
    narrow anything. The row narrowing belongs to `approvals/repo.py` and
    `decide_approval`, because a gate can only answer "at all" -- it runs
    before any row is loaded and has no department to check against.

    Do NOT commit in here. `scope_for_principal` does not, on purpose: a
    commit inside `tenant_session` unbinds `app.tenant_id` for the rest of the
    request, and the whole route body after this dependency would then
    silently see no rows at all rather than fail.
    """
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}; add it to oc8.authz.permissions")
    if or_tenant_wide is not None and or_tenant_wide not in ALL_PERMISSIONS:
        raise ValueError(f"unknown permission {or_tenant_wide!r}; add it to oc8.authz.permissions")
    _or_tenant_wide_box = (or_tenant_wide,)

    async def _dep(request: Request, principal: CurrentPrincipal, db: DbSession) -> HumanActor:
        or_tenant_wide = _or_tenant_wide_box[0]
        try:
            # `upsert=True`: the first workspace request is what mints the person,
            # which is what keeps `approval_request.decided_by` from being NULL
            # and what lets `POST /members` offer subjects the system has actually
            # seen instead of asking an admin to type an id by hand.
            member, scope = await scope_for_principal(db, principal, upsert=True)
        except PermissionError as exc:
            # A non-operator principal (a plugin token; an agent token is already
            # refused at the router by `deny_agent_principals`). Translated into
            # a 403 rather than allowed to escape as a 500: an authorization
            # refusal that pages somebody at 03:00 gets muted, and a muted refusal
            # gets loosened.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        authority = await _authority(request, db, principal)
        admitted = (
            permission in authority.tenant_wide
            or scope.holds_anywhere(permission)
            or (or_tenant_wide is not None and or_tenant_wide in authority.tenant_wide)
        )
        if not admitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires permission: {permission}",
            )
        if member is None:  # pragma: no cover - `upsert=True` raises instead
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires permission: {permission}",
            )
        return HumanActor(principal=principal, member=member, scope=scope)

    return _dep


async def holds_departmental(
    request: Request, db: AsyncSession, actor: HumanActor, permission: str
) -> bool:
    """Would `require_departmental(permission)` have admitted this actor on its own?

    For a route gated with `or_tenant_wide` that must still tell the two kinds
    of caller apart inside its body. The approval routes are the case: they
    admit a member holding only `copilot:use` so the person a member-routed
    approval (D3a) was assigned to can reach it, and everything that is NOT
    such a row must answer that member exactly as the narrower gate did.
    Same formula as `_dep` above, off the same memoised `Authority`.
    """
    authority = await _authority(request, db, actor.principal)
    return permission in authority.tenant_wide or actor.scope.holds_anywhere(permission)


def require_agent_write() -> Callable[..., Awaitable[HumanActor]]:
    """The coarse door for `agent:manage`-parity write routes. Decision 1
    originally named exactly six: `POST /agents` plus five `agents_write.py`
    mutations (lifecycle, narrowing, runtime, model-config, skills) --
    `test_every_route_is_governed.py::_AGENT_WRITE_ROUTES` still pins that
    original six by name. Every genuine mutation added to `agents_write.py`
    since (instructions, memory delete, agent delete/restore, and Task 11's
    `upload_instruction_file`) sits behind this same door too, just outside
    that pinned set -- so "six" names decision 1's original list, not the
    door's current total traffic. The sibling GET, `list_instruction_files`,
    is a plain read and does NOT sit behind this door: it lives in
    `agents.py` behind `require_departmental(perm(AGENT, VIEW))` instead --
    see that function's own docstring, and `files.py`'s `_owned_attachment`
    for why a list read must not be gated more strictly than downloading or
    deleting one of the same files by id already is.

    Admits a caller who may write SOME agent SOMEWHERE -- tenant-wide
    `agent:manage`, or a live seat whose `agent_manage` column is TRUE in at
    least one department. Narrows nothing: which department is not yet known
    at the door (a `POST /agents` body has not been parsed, an `agent_id` in
    the path has not been loaded), so this can only ask "at all", the same
    shape as `require_departmental`. The per-resource narrow is
    `authorize_agent_write`, called from inside the route body once the target
    department is known.

    Deliberately its own gate and not `require_departmental(perm(AGENT,
    MANAGE))`: that call would raise at import (`agent:manage` is not, and
    must never become, a member of `SEAT_PERMISSIONS` -- decision 2). Neither
    this function nor `authorize_agent_write` takes a permission-string
    argument anywhere in its signature or its body, which is what makes
    `agent:manage` structurally -- not just conventionally -- unreachable
    through the tenant-defined-role catalogue: there is no `perm()` call here
    for a role-builder's validation to find.
    """

    async def _dep(request: Request, principal: CurrentPrincipal, db: DbSession) -> HumanActor:
        try:
            # `upsert=True`, same reasoning as `require_departmental`: the first
            # request through this door is what mints the person, and a write
            # route is exactly the kind of request `decided_by`/`granted_by`
            # style attribution must never see as NULL.
            member, scope = await scope_for_principal(db, principal, upsert=True)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

        authority = await _authority(request, db, principal)
        tenant_wide = perm(AGENT, MANAGE) in authority.tenant_wide
        if not (tenant_wide or scope.agent_manage_departments) or member is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires permission: {perm(AGENT, MANAGE)}",
            )
        return HumanActor(principal=principal, member=member, scope=scope)

    return _dep


async def authorize_agent_write(
    request: Request,
    db: AsyncSession,
    actor: HumanActor,
    department_id: uuid.UUID,
    *,
    not_found: HTTPException,
) -> None:
    """The per-resource narrow: may `actor` write an agent in `department_id`?

    **MUST be the first statement in the route body after `department_id`
    becomes knowable -- strictly BEFORE any frame-derived computation**
    (`narrowing_within_frame`, `missing_skill_requirements`, or anything else
    that reads the department's tool frame). This is the single most severe
    finding across all three source designs' reviews: a wrong-department
    toggle holder who reaches a 422 before the refusal fires can reconstruct
    that department's entire tool frame, one crafted request's violation list
    at a time. Calling this late trusts every future call site to remember an
    ordering rule; calling it first removes the rule from the set of things a
    call site can get wrong.

    `not_found` is the route's OWN existing not-found exception -- same status,
    same text as a genuinely missing resource -- never invented fresh here, so
    a foreign-department refusal is byte-identical to a truly nonexistent id
    and neither confirms a resource's existence to a caller who cannot reach
    it nor invites a route-specific 403/404 drift between call sites.
    """
    authority = await _authority(request, db, actor.principal)
    if perm(AGENT, MANAGE) in authority.tenant_wide:
        return
    if department_id in actor.scope.agent_manage_departments:
        return
    if department_id not in actor.scope.viewable:
        raise not_found
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"requires {perm(AGENT, MANAGE)} in this department",
    )


def unguarded(reason: str) -> Callable[[], None]:
    """Mark a route that deliberately carries no operator permission.

    Not decoration: `test_every_route_is_governed` treats an unmarked route
    without a permission as a defect, so this is the only way to leave one
    open -- and it costs a sentence saying why. The routes that legitimately
    qualify are authenticated by something OTHER than an operator's role: a
    webhook signature, an OAuth state parameter, a run-scoped agent token, or
    nothing at all because they exist to be reachable before login.
    """

    def _dep() -> None:
        return None

    _dep.__doc__ = reason
    _dep.oc8_unguarded_reason = reason  # type: ignore[attr-defined]
    return _dep


def require_scope(*scopes: str) -> Callable[[Principal], Awaitable[Principal]]:
    """For ``kind=="plugin"`` principals, 403 unless every scope is granted.

    Non-plugin principals (operators, agents) pass through unchecked — they're
    still governed by ``require_role`` where applicable.
    """

    async def _dep(principal: CurrentPrincipal) -> Principal:
        if principal.kind == "plugin":
            missing = [s for s in scopes if s not in principal.scopes]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"missing scopes: {', '.join(missing)}",
                )
        return principal

    return _dep
