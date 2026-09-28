"""Dev and local password authentication endpoints.

`dev-login` mints a token for a seeded tenant so the frontend can authenticate
during early development, without going through the real setup/login flow.

Local password authentication (WP-B) provides `setup`, `login`, and `logout` endpoints
for Community single-instance deployments. These operate alongside the existing dev flow --
the only two identity paths this edition ships.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.api.v1._serializers import member_to_dto, seat_to_dto
from oc8.api.v1.members import _member_dto
from oc8.audit import append_event
from oc8.auth import Principal, get_identity_provider
from oc8.auth.password import PasswordHashingError, hash_password, verify_password
from oc8.auth.totp_gate import totp_gate
from oc8.authz.permissions import MEMBER_ROLE
from oc8.authz.scope import scope_for_principal, subject_uuid_for
from oc8.config import get_settings
from oc8.constants import ACME_TENANT_ID, DEV_OPERATOR_SUBJECT
from oc8.db.session import owner_session, tenant_session
from oc8.mail.send import active_smtp_credential, deliver, resolve_smtp_config
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import AuthConfig, MeDTO, MemberDTO
from oc8.schemas.requests import (
    ChangeOwnEmailRequest,
    ChangeOwnPasswordRequest,
    ConfirmEmailChangeRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    UpdateDisplayNameRequest,
)
from oc8.tenants.provision import (
    TenantExists,
    create_tenant,
    get_singleton_organization,
    list_tenants,
)
from oc8.workspace.members import list_members, seats_for

router = APIRouter()

_COMMUNITY_INITIAL_SLUG = "oc8-community"
_COMMUNITY_INITIAL_NAME = "OC8 Community"

#: How long after a link is minted a further request for the same member is
#: answered WITHOUT minting or mailing a second one (design §3: "a simple
#: per-subject cooldown on the public endpoints").
#:
#: Both mailing routes need it, for one reason: spending the previous token --
#: which is all that was here before -- bounds how many links are USABLE (one),
#: not how many emails are SENT. Without a cooldown, anybody who knows an
#: address can drive `/auth/password/forgot` in a loop, unauthenticated, and
#: fill that inbox for ever, at one outbound SMTP dial per request.
#:
#: 60 seconds, because the window has to be short enough that somebody who
#: genuinely did not receive the first mail retries successfully within the
#: time they would spend looking for it, and long enough that a scripted loop
#: collapses to one mail a minute. It is not a general rate limiter and does
#: not pretend to be one: it is per member, and a real limiter (per IP, at the
#: proxy) is a deployment concern this process cannot see.
VERIFICATION_MAIL_COOLDOWN_SECONDS = 60


class DevLoginRequest(CamelModel):
    """A `CamelModel`, and that is the whole fix.

    It was a plain `BaseModel` with a snake_case `tenant_id` while the frontend --
    like every other body in this API -- sends `tenantId`. Pydantic ignored the
    unknown key and fell back to the default, so EVERY dev token was minted for
    ACME whatever was clicked in the tenant picker. That is not cosmetic for a
    department-scoped workspace: a seat granted in another tenant is invisible to
    an ACME token, and the screen shows an empty queue that looks exactly like the
    failure it is meant to be distinguishable from.

    `populate_by_name` is on (`CamelModel`), so the snake_case bodies that already
    exist keep working.
    """

    tenant_id: uuid.UUID = ACME_TENANT_ID
    role: str = "org_admin"
    subject: str = DEV_OPERATOR_SUBJECT


class TokenResponse(BaseModel):
    token: str
    principal: Principal


class DevTenantRequest(BaseModel):
    name: str
    slug: str
    region: str = "eu"


class DevTenantDTO(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    region: str


def _dev_only() -> None:
    if not get_settings().is_dev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.post(
    "/auth/dev-login",
    response_model=TokenResponse,
    dependencies=[
        Depends(unguarded("exists to be reachable without a token; PIN-gated and dev-only"))
    ],
)
async def dev_login(body: DevLoginRequest) -> TokenResponse:
    _dev_only()
    provider = get_identity_provider()
    token = provider.mint(tenant_id=body.tenant_id, subject=body.subject, role=body.role)
    principal = provider.verify(token)
    return TokenResponse(token=token, principal=principal)


@router.get(
    "/auth/dev-tenants",
    response_model=list[DevTenantDTO],
    dependencies=[Depends(unguarded("dev login helper, reachable before any token exists"))],
)
async def dev_tenants() -> list[DevTenantDTO]:
    _dev_only()
    async with owner_session() as db:
        return [
            DevTenantDTO(id=row.tenant_id, name=row.name, slug=row.slug, region=row.region)
            for row in await list_tenants(db)
        ]


@router.get(
    "/auth/dev-members",
    response_model=list[MemberDTO],
    dependencies=[Depends(unguarded("dev login helper, reachable before any token exists"))],
)
async def dev_members(tenant_id: uuid.UUID = ACME_TENANT_ID) -> list[MemberDTO]:
    """Who has already shown up in this tenant, for the sign-in picker.

    `tenant_id` is a plain query parameter, not a `CamelModel` body field --
    FastAPI does not camelCase those on its own, so it stays snake_case on the
    wire (`?tenant_id=...`), unlike every JSON body in this API.
    `test_dev_login_honours_the_requested_tenant.py` exists because the
    equivalent body field silently fell back to its default when the frontend
    sent `tenantId`; a query param has the same trap in the other direction --
    sending `tenantId` here would silently return ACME's list instead.

    `list_members` already batches seats and role names in one pass (see its
    own docstring on why); this is a thin, dev-only, pre-token wrapper around
    it, over the owner session for the same reason `dev_tenants` is -- there is
    no principal yet to bind an RLS session to.

    Logging a listed person back in always mints them the token role `member`,
    never their tenant role: that IS the design (§0.A) -- a human's token holds
    nothing, their authority is seats and an assigned tenant role, read live on
    every request. `dev-login`'s own `role` field stays reachable directly for
    the quick-access buttons that test the built-in ladder without a seat.
    """
    _dev_only()
    async with owner_session() as db:
        rows, _total = await list_members(db, tenant_id=tenant_id)
        return [member_to_dto(r) for r in rows]


@router.post(
    "/auth/dev-tenants",
    response_model=DevTenantDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(unguarded("dev login helper, reachable before any token exists"))],
)
async def create_dev_tenant(body: DevTenantRequest) -> DevTenantDTO:
    """Local-only convenience around the same owner-session provisioning used by the CLI.

    Community is single-tenant in production: the real onboarding path is
    ``POST /auth/setup`` against the one bootstrapped organization, not this
    dev-only tenant picker.
    """
    _dev_only()
    try:
        async with owner_session() as db:
            created = await create_tenant(
                db, slug=body.slug, name=body.name, region=body.region, department_name="Sales"
            )
            await db.commit()
            return DevTenantDTO(
                id=created.tenant_id, name=body.name, slug=created.slug, region=body.region
            )
    except TenantExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/me",
    response_model=MeDTO,
    dependencies=[
        Depends(
            unguarded(
                "returns only the caller's own identity and the seats they "
                "themselves hold, which every caller may see"
            )
        )
    ],
)
async def me(principal: CurrentPrincipal, db: DbSession) -> MeDTO:
    """The caller, and where they stand.

    Additive over what this returned before -- `subject`, `role` and `kind` are
    the three fields the frontend reads and none of them moved. What is new is
    the pair the workspace turns on: `seats` and `viewsAllDepartments`.

    Without them the screen cannot tell "nothing is waiting for you" from "nobody
    has put you in a department yet". Those are the same blank page today, and one
    of them is the system working while the other is a person locked out of their
    own job.

    The member row is upserted here, on a GET, and that is deliberate: a person's
    first request is what mints them, which is what keeps
    `approval_request.decided_by` from being NULL and lets `POST /members` offer
    subjects the system has actually seen instead of asking an administrator to
    type an id by hand.
    """
    org = await db.get(m.Organization, principal.tenant_id)
    onboarding_status = org.onboarding_status if org is not None else None
    try:
        member, scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError:
        # A principal that cannot stand in a department at all (a plugin token;
        # an agent token never reaches the operator API). It still gets its own
        # identity back -- this route is how a caller finds out what it is -- but
        # with no member and no seats rather than a 500.
        return MeDTO(
            subject=principal.subject,
            tenant_id=str(principal.tenant_id),
            role=principal.role,
            kind=principal.kind,
            scopes=list(principal.scopes),
            onboarding_status=onboarding_status,
        )

    # `include_archived=False`: this answers WHERE THIS PERSON STANDS, and
    # `authz/scope.py` stopped counting a seat in an archived department when it
    # started joining `Department`. A seat listed here that grants nothing would
    # put the workspace in the wrong empty state -- "nothing is waiting for you"
    # instead of "nobody has assigned you to a department". `GET /members` still
    # sees it, because that is the screen that has to revoke it.
    seats = await seats_for(db, member.id, include_archived=False) if member is not None else []
    return MeDTO(
        subject=principal.subject,
        tenant_id=str(principal.tenant_id),
        role=principal.role,
        kind=principal.kind,
        scopes=list(principal.scopes),
        display_name=member.display_name if member is not None else "",
        member_id=str(member.id) if member is not None else None,
        # The scope's own answer, not `role == "org_admin"`: an auditor holds
        # `approval:view_any` and an `all_departments` row-CEO holds no role at
        # all, and both of them see every department.
        views_all_departments=scope.is_unrestricted,
        # BOTH flags, because they are two different people. Sending only the
        # first made the screen offer an auditor a button the backend refuses on
        # every click -- the answer was in the scope and simply never left the
        # process.
        decides_all_departments=scope.decides_everywhere,
        seats=[seat_to_dto(s) for s in seats],
        onboarding_status=onboarding_status,
    )


# =============================================================================
# Self-service account settings
# =============================================================================
# Three routes a person may run on THEMSELVES without holding `member:manage`.
# Every one of them resolves the member row from the caller's own principal --
# there is deliberately no `{member_id}` path parameter anywhere below, because
# that parameter is the whole difference between "change my own password" and
# "change anybody's password", and the second one already exists, admin-gated,
# in `api/v1/members.py`. `unguarded(...)` is honest here for exactly that
# reason: the authorization IS the token's own subject.
#
# Design: docs/superpowers/specs/2026-08-28-account-self-service-design.md.

#: How long a mailed email-change confirmation link stays usable. Short on
#: purpose: the link, in the wrong inbox, moves somebody's login.
EMAIL_CHANGE_TOKEN_TTL = dt.timedelta(hours=1)


async def _own_member_or_404(db: AsyncSession, principal: Principal) -> m.OrgMember:
    """The caller's own `org_member` row, minted on first sight.

    `upsert=True` for the same reason `GET /me` uses it: a person's first
    request is what creates their row, and a self-service screen must not be
    the one place that says "no such account" to somebody holding a valid
    session.

    A non-operator principal (an agent or plugin token) has no account to
    edit at all; `scope_for_principal` raises for it and that becomes a 403,
    not the 500 an uncaught `PermissionError` would be.
    """
    try:
        member, _scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this token does not belong to a person with an account"
        ) from exc
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no member record for this caller")
    return member


async def _mail_is_cooling_down(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    purpose: str,
    new_email: str | None = None,
) -> bool:
    """Was a link of this `purpose` minted for this member moments ago?

    True means the caller already has a live link in the inbox this request
    would mail again -- so nothing is minted and nothing is sent, and the
    caller is told exactly what they would have been told anyway (see
    `VERIFICATION_MAIL_COOLDOWN_SECONDS`).

    Only UNUSED tokens count: a link that has already been spent is not
    sitting in anybody's inbox, and the person who spent it and now wants
    another one is not the bombing case this exists for.

    `created_at` is compared against this process's clock while the column
    itself carries PostgreSQL's `now()` default. That is the same mixed pair
    `_redeem_token` already lives with on `expires_at`, and a window this
    coarse (60s) does not care about the millisecond of skew between a
    backend and its database.

    `new_email` narrows the window to ONE target address, and only the
    email-change route passes it. Correcting a mistyped new address is the
    common, legitimate second request there, and answering it with "check your
    inbox" -- pointing at the typo -- would be a trap. The abuse this stops
    (one inbox, mailed in a loop) is unaffected: bombing needs the address
    fixed. Forgot-password has no such second address and passes nothing.
    """
    minted_after = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
        seconds=VERIFICATION_MAIL_COOLDOWN_SECONDS
    )
    stmt = (
        select(m.AccountVerificationToken.id)
        .where(
            m.AccountVerificationToken.tenant_id == tenant_id,
            m.AccountVerificationToken.member_id == member_id,
            m.AccountVerificationToken.purpose == purpose,
            m.AccountVerificationToken.used_at.is_(None),
            m.AccountVerificationToken.created_at > minted_after,
        )
        .limit(1)
    )
    if new_email is not None:
        stmt = stmt.where(m.AccountVerificationToken.new_email == new_email)
    return (await db.execute(stmt)).first() is not None


async def _member_by_sign_in_address(
    db: AsyncSession, *, tenant_id: uuid.UUID, email: str
) -> m.OrgMember | None:
    """The live member who signs in with `email`, casing ignored.

    `org_member.subject` IS the login, and until email addresses were
    normalized (`ChangeOwnEmailRequest`/`ForgotPasswordRequest` lowercase
    theirs) nothing stopped one instance from holding `Ada@x.com` and
    `ada@x.com` as two separate accounts -- `(tenant_id, subject)` is unique
    byte-for-byte. So rows written before that may carry any casing, and an
    exact-match lookup would leave those people unable to log in or to ask for
    a reset the moment the INPUT side started normalizing. Comparing on
    `lower()` is what keeps them reachable.

    Ambiguity is resolved, never guessed: if two rows differ only in case, the
    one spelled EXACTLY as typed wins, and if none is, this answers `None`
    (a 401 / a generic 202) rather than picking one at random or raising a
    500 out of `scalar_one_or_none`.
    """
    typed = email.strip()
    candidates = list(
        (
            await db.execute(
                select(m.OrgMember).where(
                    m.OrgMember.tenant_id == tenant_id,
                    func.lower(m.OrgMember.subject) == typed.lower(),
                    m.OrgMember.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(candidates) == 1:
        return candidates[0]
    return next((c for c in candidates if c.subject == typed), None)


def _verified_or_401(member: m.OrgMember, current_password: str) -> None:
    """Re-prove the password before anything that moves the sign-in identity.

    A member with no `password_hash` (SSO/dev-token identity) has no current
    password to confirm, so these two routes are simply closed to them --
    same 401, so the response never reveals which of the two it was.
    """
    if member.password_hash is None or not verify_password(current_password, member.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")


@router.put(
    "/auth/me/display-name",
    response_model=MemberDTO,
    dependencies=[Depends(unguarded("self-service: acts only on the caller's own member row"))],
)
async def update_own_display_name(
    body: UpdateDisplayNameRequest, db: DbSession, principal: CurrentPrincipal
) -> MemberDTO:
    """Rename yourself as the workspace shows you.

    No password confirmation: this is the one self-service field that grants
    nothing and unlocks nothing. It is still audited, because a display name
    is what every approval and audit row names a person by on screen.
    """
    member = await _own_member_or_404(db, principal)
    display_name = body.display_name.strip()
    # `min_length=1` passes a string of spaces, which strips to "" -- and an
    # empty display name is what the nav bar, every approval row and every
    # audit line names this person by. Refused rather than stored blank.
    if not display_name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A display name cannot be blank."
        )
    previous = member.display_name
    member.display_name = display_name
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=member.id,
        category="member",
        action="member.display_name_changed",
        resource={
            "member_id": str(member.id),
            "previous_display_name": previous,
            "display_name": member.display_name,
        },
        reason="changed by the member themselves",
        principal=principal,
    )
    # Built before the commit: a commit inside `tenant_session` unbinds
    # `app.tenant_id`, so the seat query behind this DTO would come back empty
    # afterwards and the response would say the person holds nothing. Same
    # ordering as every writer in `api/v1/members.py`, for the same reason.
    dto = await _member_dto(db, member)
    await db.commit()
    return dto


@router.put(
    "/auth/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(unguarded("self-service: acts only on the caller's own member row"))],
)
async def change_own_password(
    body: ChangeOwnPasswordRequest, db: DbSession, principal: CurrentPrincipal
) -> None:
    """Change your own password, having first proven you know the old one.

    The current-password check is not a formality: without it, anyone who
    borrows an unlocked browser could set a new password and lock the owner
    out of their own account without ever knowing the old one. Answers 204 --
    the hash is never echoed, and neither password appears in any audit row.
    """
    member = await _own_member_or_404(db, principal)
    _verified_or_401(member, body.current_password)
    try:
        member.password_hash = hash_password(body.new_password)
    except PasswordHashingError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Could not hash password."
        ) from exc
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=member.id,
        category="member",
        action="member.password_changed",
        resource={"member_id": str(member.id)},
        reason="changed by the member themselves",
        principal=principal,
    )
    await db.commit()


class EmailChangeResponse(CamelModel):
    """Which of the two branches `PUT /auth/me/email` took."""

    #: True when a confirmation email was sent INSTEAD of applying the change
    #: -- the frontend swaps its form for "check your inbox" on this flag.
    verification_required: bool
    #: Echoed back only on the verification branch, so the screen can name the
    #: inbox to go and look in.
    sent_to: str | None = None
    #: The updated member, present only when the change applied immediately.
    member: MemberDTO | None = None
    #: True when the caller's CURRENT session no longer matches their identity
    #: and they must sign in again. **The frontend must act on this, not just
    #: render it**: discard the token and redirect to /login.
    #:
    #: Sessions are stateless JWTs carrying the subject as a claim, and this
    #: endpoint has no way to revoke one. So the moment the immediate-apply
    #: branch rewrites `org_member.subject`, the token in the caller's browser
    #: names an identity that no longer has a row -- and the next authenticated
    #: request runs `scope_for_principal(upsert=True)`, which INSERTS a fresh
    #: empty member under the old subject: no seats, no role, no password. The
    #: person is then silently signed in as a ghost of themselves with none of
    #: their access, and that ghost holds `(tenant_id, old_subject)` for good,
    #: so changing the address back later 409s against their own leftovers.
    #:
    #: Signing out immediately is what avoids all of it. Left False on the
    #: verification branch, where nothing has moved yet and the session is
    #: still perfectly valid.
    reauth_required: bool = False


@router.put(
    "/auth/me/email",
    response_model=EmailChangeResponse,
    dependencies=[Depends(unguarded("self-service: acts only on the caller's own member row"))],
)
async def change_own_email(
    body: ChangeOwnEmailRequest, db: DbSession, principal: CurrentPrincipal
) -> EmailChangeResponse:
    """Change your own sign-in identity, in one of two ways.

    The email IS the login here (`org_member.subject`), so this is the most
    dangerous of the three self-service routes and it branches on a fact
    about the deployment rather than on a preference:

    * **No mail server configured** -- the change applies immediately. There
      is no way to prove the new address is reachable and no way to tell the
      person if it is not, so refusing here would leave a self-hosted
      instance with no way to correct a typo'd login at all, short of an
      administrator. `subject` and `subject_uuid` are rewritten together,
      exactly as `PUT /members/{id}/subject` does -- the second is a pure
      function of the first, and leaving the old one behind desyncs the row
      from the identity check the rename exists to update. The response
      carries `reauthRequired: true` on this branch and the frontend MUST
      sign the caller out on it; see `EmailChangeResponse.reauth_required`
      for what happens to a session that is allowed to survive.
    * **Mail server configured** -- nothing on the member row moves yet. A
      single-use token is stored (hashed; the plaintext is mailed once and
      never persisted) and the change lands only when the link comes back to
      `POST /auth/confirm-email`. That is what stops a typo -- or a
      deliberately hostile address -- from silently becoming the only way
      into this account.

    Either way the address is refused with a 409 if another live member in
    this tenant already signs in with it: `(tenant_id, subject)` is unique,
    and the row that lost that race would look like it had simply vanished.

    On the mail-server branch a repeat request for the SAME address inside
    `VERIFICATION_MAIL_COOLDOWN_SECONDS` mints and mails nothing, and answers
    with the "check your inbox" it would have answered anyway -- which is true,
    because the link from a moment ago is still live. A different address is
    never held back, so correcting a typo is instant.
    """
    member = await _own_member_or_404(db, principal)
    _verified_or_401(member, body.current_password)

    # Already validated as an address and lowercased by the request model --
    # `org_member.subject` IS the login, so a value that is not an address (or
    # that differs from an existing row only in casing) is not a cosmetic
    # problem here. The strip is belt and braces: the normalizer does it too,
    # and this line is what stops a future edit to that model from quietly
    # reaching the immediate-apply branch below with whitespace in it.
    new_email = body.new_email.strip()
    if not new_email:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "An email address cannot be blank."
        )
    if new_email == member.subject:
        # Nothing to change. Falling through would either write an audit row
        # claiming a rename that did not happen, or -- on the branch below --
        # mail a confirmation link for the address the person already uses.
        dto = await _member_dto(db, member)
        return EmailChangeResponse(verification_required=False, member=dto)

    clash = (
        await db.execute(
            select(m.OrgMember.id).where(
                m.OrgMember.tenant_id == principal.tenant_id,
                m.OrgMember.subject == new_email,
                m.OrgMember.deleted_at.is_(None),
                m.OrgMember.id != member.id,
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Another user already signs in with that identity."
        )

    if await active_smtp_credential(db, tenant_id=principal.tenant_id) is None:
        old_subject = member.subject
        member.subject = new_email
        member.subject_uuid = subject_uuid_for(new_email)
        await append_event(
            db,
            tenant_id=principal.tenant_id,
            actor_type="operator",
            actor_id=member.id,
            category="member",
            action="member.subject_renamed",
            resource={
                "member_id": str(member.id),
                "previous_subject": old_subject,
                "subject": new_email,
            },
            reason="changed by the member themselves; no mail server configured to confirm it",
            principal=principal,
        )
        dto = await _member_dto(db, member)
        await db.commit()
        # `reauth_required`: the caller's own token still carries the OLD
        # subject and nothing here can revoke it. See the field's own comment
        # -- the next request would mint them a ghost member row.
        return EmailChangeResponse(verification_required=False, member=dto, reauth_required=True)

    if await _mail_is_cooling_down(
        db,
        tenant_id=principal.tenant_id,
        member_id=member.id,
        purpose="email_change",
        new_email=new_email,
    ):
        # A link for THIS address was mailed seconds ago and is still live, so
        # the answer is the true one the caller already has: go and look. See
        # `VERIFICATION_MAIL_COOLDOWN_SECONDS` -- without this, a held session
        # mails somebody's inbox once per click.
        return EmailChangeResponse(verification_required=True, sent_to=new_email)

    # Resolve the mail server BEFORE writing the token, because a credential
    # that cannot be resolved is a 502 and must leave nothing behind -- and
    # this read needs the transaction still open and still RLS-bound
    # (`app.tenant_id` dies at commit; the vaulted password would come back
    # empty afterwards). See oc8.mail.send's module docstring.
    smtp = await resolve_smtp_config(db, tenant_id=principal.tenant_id)
    if smtp is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not send the confirmation email. Check the mail server settings.",
        )

    token = secrets.token_urlsafe(32)
    row = m.AccountVerificationToken(
        tenant_id=principal.tenant_id,
        member_id=member.id,
        purpose="email_change",
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        new_email=new_email,
        expires_at=dt.datetime.now(tz=dt.UTC) + EMAIL_CHANGE_TOKEN_TTL,
    )
    db.add(row)
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=member.id,
        category="member",
        action="member.email_change_requested",
        resource={
            "member_id": str(member.id),
            "subject": member.subject,
            "requested_subject": new_email,
        },
        reason="awaiting confirmation of the new address",
        principal=principal,
    )
    await db.flush()
    token_id = row.id
    # Commit BEFORE the socket, which is the opposite of what this used to do.
    # Sending first kept RLS bound (see above) but held this request's pooled
    # DB connection for however long the relay took to answer -- up to ~40
    # seconds of per-operation timeouts. `resolve_smtp_config` above already
    # took everything the send needs out of the database, so the connection can
    # go back to the pool now and `deliver` dials with nothing checked out.
    await db.commit()
    base = get_settings().frontend_base_url.rstrip("/")
    sent = await deliver(
        smtp,
        to=new_email,
        subject="Confirm your new email address",
        body=(
            f"Click this link to confirm your new email address:\n"
            f"{base}/confirm-email?token={token}\n\n"
            "This link expires in 1 hour. If you didn't request this, ignore this email."
        ),
    )
    if not sent:
        # A configured mail server that could not deliver. Answering "check
        # your inbox" would be a lie the person cannot act on, so this is still
        # a 502 -- but the token is already committed and cannot be rolled back
        # with the request any more, so it is SPENT instead, which leaves
        # exactly what a rollback left: nothing anybody can use. Spending it
        # also frees the retry: an unused row inside the cooldown window would
        # make the caller's immediate second attempt answer "check your inbox"
        # for mail that never left.
        #
        # A fresh session, not `db`: this one committed a moment ago, and
        # `app.tenant_id` is transaction-local, so `db` is no longer bound to
        # anything and the UPDATE would silently match zero rows.
        async with tenant_session(principal.tenant_id) as cleanup:
            await cleanup.execute(
                update(m.AccountVerificationToken)
                .where(m.AccountVerificationToken.id == token_id)
                .values(used_at=dt.datetime.now(tz=dt.UTC))
            )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not send the confirmation email. Check the mail server settings.",
        )
    return EmailChangeResponse(verification_required=True, sent_to=new_email)


@router.get(
    "/auth/config",
    response_model=AuthConfig,
    dependencies=[
        Depends(unguarded("read before login: tells the browser which identity provider to use"))
    ],
)
async def auth_config() -> AuthConfig:
    """Which identity provider the browser should use, checked before login.

    Two modes: `is_dev` (env == "dev") is the SAME flag that gates
    `dev-login`/`dev-tenants`/`dev-members` behind a 404 in `_dev_only()` --
    so this stays consistent with what those endpoints actually allow. A
    self-hosted deployment with env != "dev" has no dev endpoints: it must
    report "community" so the frontend routes to /login (real password auth)
    instead of falling through to the dev persona picker, which would be
    unreachable there.
    """
    s = get_settings()
    sso_logout_url = get_identity_provider().logout_url()
    if s.is_dev:
        return AuthConfig(
            mode="dev",
            auth_server_url="",
            realm="",
            client_id="",
            demo=s.is_demo,
            sso_logout_url=sso_logout_url,
        )
    return AuthConfig(
        mode="community",
        auth_server_url="",
        realm="",
        client_id="",
        initialized=await _instance_has_an_administrator(),
        demo=s.is_demo,
        sso_logout_url=sso_logout_url,
    )


async def _instance_has_an_administrator() -> bool:
    """Whether `POST /auth/setup` would refuse because setup already ran.

    Deliberately the SAME predicate that endpoint uses -- a member count above
    zero on the singleton organization -- so the login page cannot offer a
    setup form that setup itself will reject.

    Unresolvable states (no organization yet, or more than one) answer
    `False`. That routes the browser to the setup form, which is where the
    specific 404/409 explaining the real problem comes from; a login form
    would just fail to authenticate against a database that has no one to
    authenticate.

    No `DbSession` parameter, for the reason spelled out on `/auth/setup`:
    this runs before any token can exist, so the session is opened by hand.
    """
    try:
        async with tenant_session(None) as unbound_db:
            org = await _get_singleton_organization(unbound_db)
        async with tenant_session(org.id) as db:
            return await _count_members_in_org(db, org.id) > 0
    except HTTPException:
        return False


# =============================================================================
# Local Password Authentication (WP-B) — Community Single-Instance Only
# =============================================================================
# These endpoints implement real local password authentication for Community
# self-hosted deployments. They operate alongside the existing dev flow and
# must not break it.
#
# Spec references: oc8_auth_and_multitenancy_split_spec.md §4.1, §8

#: The grace window itself now lives with the decision that reads it, in
#: `oc8.auth.totp_gate.TOTP_GRACE_DAYS` -- a second copy of the deadline
#: here would be a second deadline.


class PasswordSetupRequest(CamelModel):
    """Initial admin setup: create the first member with a password."""

    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=1024)
    display_name: str = Field(default="", max_length=255)


class PasswordLoginRequest(CamelModel):
    """Authenticate an existing member via password."""

    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=1024)


class PasswordSessionResponse(CamelModel):
    """Response from login/setup: a new session token and caller info."""

    token: str
    principal: Principal
    member_id: str
    #: Standalone 2FA design. Non-null only on a full-session response for a
    #: mandatory org_admin who hasn't enrolled yet; the frontend uses it to
    #: show a grace-period nag.
    totp_grace_expires_at: str | None = None
    #: True when `token` above is a NARROW totp:challenge-scoped token, not
    #: a real session -- the caller must POST /auth/totp/verify next.
    requires_totp_code: bool = False
    #: True when `token` above is a NARROW totp:enroll-scoped token -- the
    #: caller must POST /auth/totp/enroll then /confirm next.
    requires_totp_enrollment: bool = False


class PasswordSessionInfo(CamelModel):
    """Current session info (used by GET /auth/session)."""

    authenticated: bool
    member_id: str | None = None
    email: str | None = None
    display_name: str | None = None


_get_singleton_organization = get_singleton_organization


async def _count_members_in_org(db: AsyncSession, org_id: uuid.UUID) -> int:
    """Count non-soft-deleted members in an Organization."""
    result = await db.execute(
        select(func.count(m.OrgMember.id)).where(
            m.OrgMember.tenant_id == org_id,
            m.OrgMember.deleted_at.is_(None),
        )
    )
    return result.scalar() or 0


async def _bootstrap_community_organization_if_empty() -> None:
    """Create Community's one instance root only for a genuinely empty database.

    Migrations intentionally create schema but not customer data.  The first
    unauthenticated setup request is therefore the one permitted place to
    create the singleton root; ``create_tenant`` supplies its required baseline
    roles, department, templates, and audit event.  Existing roots still go
    through the normal fail-closed singleton validation in ``password_setup``.
    """
    async with owner_session() as db:
        existing = (await db.execute(select(m.Organization.id).limit(2))).scalars().all()
        if existing:
            return
        await create_tenant(
            db,
            slug=_COMMUNITY_INITIAL_SLUG,
            name=_COMMUNITY_INITIAL_NAME,
            department_name="General",
        )
        await db.commit()


@router.post(
    "/auth/setup",
    response_model=PasswordSessionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(unguarded("setup is callable before any token exists; checked by endpoint"))
    ],
)
async def password_setup(body: PasswordSetupRequest) -> PasswordSessionResponse:
    """Initialize a Community/Enterprise instance with a local admin.

    POST /auth/setup is idempotent per-subject but fails if:
    - Any members already exist in the Organization (instance already set up)
    - Organization does not exist or multiple Organizations exist
    - Password is invalid (too short/long)

    This endpoint creates the first org_member and mints an initial JWT token
    bound to the singleton Organization ID. No further setup is required;
    the frontend may proceed to /welcome or redirect to /dashboard.

    Email is stored as-is in org_member.subject (not normalized). The password
    is hashed with Argon2id and stored in org_member.password_hash. The subject
    never appears in logs or error messages.

    Request: email and password (plain, min 8 chars).
    Response: token (JWT), principal (parsed claims), member_id (UUID).

    Raises:
        404: Organization not found (database not yet initialized)
        409: Multiple Organizations detected (data corruption)
        422: member_count > 0 (instance already set up)
        422: Invalid password (too short/long, hashing failed)

    No `db: DbSession` parameter here on purpose: `DbSession` resolves through
    `get_db` -> `get_principal` -> a mandatory Bearer token, which cannot exist
    yet on an empty instance. `unguarded(...)` above only exempts the ROUTE from
    the permission-governance test -- it does nothing to a `DbSession` parameter,
    which still demands a token. Sessions are opened by hand instead: first
    unbound (organization alone is readable unbound, see db/session.py), then
    bound to the resolved singleton tenant for the actual member write.
    """
    # A freshly migrated Community database has no customer rows. Bootstrap its
    # single root before the normal unbound singleton discovery below.
    await _bootstrap_community_organization_if_empty()

    # Resolve the singleton instance. Unbound read: db/session.py grants an
    # unbound session read access to `organization` specifically so this kind
    # of pre-auth bootstrap can find the one tenant to bind to.
    async with tenant_session(None) as unbound_db:
        org = await _get_singleton_organization(unbound_db)

    # Hash the password before opening the write session, so a slow hash never
    # holds the tenant-bound transaction open longer than it has to.
    try:
        password_hash = hash_password(body.password)
    except PasswordHashingError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password hashing failed. Try a different password.",
        ) from err

    async with tenant_session(org.id) as db:
        # Verify this is the first setup: 0 members allowed
        member_count = await _count_members_in_org(db, org.id)
        if member_count > 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Instance already initialized. Setup can only be run once.",
            )

        # The minted token below carries `role: "org_admin"`, but that claim
        # is only ever read when `org_member.role_id IS NULL` (authz/authority.py)
        # -- and `password_login` never re-mints "org_admin" into a token (by
        # design, see that function's own comment), only the empty MEMBER_ROLE.
        # Leaving `role_id` unset here used to mean this admin's authority
        # existed nowhere but that one, short-lived token: the next login
        # after it expired silently dropped them to zero permissions, with no
        # other admin left to grant it back. `create_tenant` always seeds a
        # builtin "org_admin" Role per tenant (tenants/provision.py), so it is
        # resolved and persisted here instead of trusted to the JWT alone.
        admin_role_id = (
            await db.execute(
                select(m.Role.id).where(
                    m.Role.tenant_id == org.id,
                    m.Role.name == "org_admin",
                    m.Role.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if admin_role_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Tenant provisioning is incomplete: no org_admin role found.",
            )

        # Create the org_member row
        # Use email as the subject (the unique identifier for this user in this instance)
        member = m.OrgMember(
            tenant_id=org.id,
            subject=body.email,  # Email as subject for local auth
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:local:{body.email}"),
            display_name=body.display_name or body.email,
            password_hash=password_hash,
            all_departments=True,  # Initial admin sees all departments
            role_id=admin_role_id,
            totp_grace_started_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add(member)
        await db.flush()
        member_id = member.id

        # The member row above just set `totp_grace_started_at`, so this is
        # always outcome 1 (full_session_ok) -- nothing can be enrolled yet
        # on a member that didn't exist a moment ago. Routed through the
        # shared `totp_gate` anyway rather than recomputing the deadline
        # inline: one copy of the grace-window arithmetic, not two (see that
        # module's own docstring on why a second copy is how this drifts).
        outcome = await totp_gate(db, member_id=member_id)

    # Mint a token for this new member
    provider = get_identity_provider()
    token = provider.mint(
        tenant_id=org.id,
        subject=body.email,
        role="org_admin",  # Initial admin
        kind="operator",
    )

    # Verify the token to return principal info
    principal = provider.verify(token)

    return PasswordSessionResponse(
        token=token,
        principal=principal,
        member_id=str(member_id),
        totp_grace_expires_at=(
            outcome.totp_grace_expires_at.isoformat()
            if outcome.totp_grace_expires_at is not None
            else None
        ),
    )


@router.post(
    "/auth/login",
    response_model=PasswordSessionResponse,
    dependencies=[
        Depends(unguarded("login is pre-auth; all checking is inside the endpoint"))
    ],
)
async def password_login(body: PasswordLoginRequest) -> PasswordSessionResponse:
    """Authenticate a member via email + password.

    POST /auth/login verifies the email and password against the stored
    Argon2id hash in org_member.password_hash. On success, a new JWT token
    is minted and returned.

    Only members with a non-NULL password_hash can use this endpoint (those
    created via /auth/setup). A member with no password_hash set receives a
    401.

    Email lookup ignores casing (`_member_by_sign_in_address`), and the minted
    token carries the STORED spelling of the subject rather than the typed one.
    Passwords are compared using constant-time verification (Argon2). Failed
    logins do not disclose whether the email exists.

    Request: email and password (plain).
    Response: token (JWT), principal (parsed claims), member_id (UUID).

    Raises:
        404: Organization not found
        401: Email not found, password mismatch, or member has no password_hash

    No `db: DbSession` parameter here either -- see password_setup's docstring
    for why: it would silently demand a Bearer token that cannot exist yet.
    """
    # Resolve the singleton instance (unbound read, see password_setup)
    async with tenant_session(None) as unbound_db:
        org = await _get_singleton_organization(unbound_db)

    # Look up the member by email, tenant-bound. Casing is ignored (and
    # ambiguity resolved by exact spelling) -- see `_member_by_sign_in_address`
    # for why an exact-match-only lookup is a lockout now that the addresses
    # written by the self-service routes are normalized to lowercase.
    async with tenant_session(org.id) as db:
        member = await _member_by_sign_in_address(db, tenant_id=org.id, email=body.email)
        member_id = member.id if member is not None else None
        password_hash = member.password_hash if member is not None else None
        # The STORED spelling, not the typed one. Every token minted below
        # carries this as its `sub` claim, and a claim that does not match the
        # row is how `scope_for_principal(upsert=True)` mints a ghost member
        # on the caller's very next request -- see `EmailChangeResponse.
        # reauth_required` for what that costs the person it happens to.
        subject = member.subject if member is not None else body.email

    # Fail closed: wrong email, missing password_hash, or soft-deleted member
    if member_id is None or password_hash is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # Verify password (constant-time comparison, returns bool not exception)
    if not verify_password(body.password, password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # The token's `role` claim only matters when `org_member.role_id IS NULL`
    # (see authz/authority.py's module docstring: an assigned role_id is an
    # OVERRIDE that makes this claim irrelevant). It used to be hardcoded to
    # "org_admin" -- meaning EVERY password-authenticated member with no
    # explicitly assigned role silently held all 52 permissions, not just the
    # ones an admin actually granted them. MEMBER_ROLE resolves to the empty
    # permission set (see permissions.py's BUILTIN_ROLE_PERMISSIONS), the
    # correct fail-closed floor: a member who should have real access gets an
    # explicit role_id via PUT /members/{id}/role, same as any other role
    # assignment in this system.
    token_role = MEMBER_ROLE

    # Standalone 2FA design: does this member need TOTP before a full
    # session? Checked AFTER password verification succeeds (never before --
    # see test_login_does_not_leak_totp_enrollment_status_before_password_is_verified),
    # so a wrong password gets byte-identical 401s whether or not TOTP is
    # involved for this member at all.
    #
    # The DECISION lives in auth/totp_gate.py. What stays HERE is the
    # MINTING: only this function knows it is issuing a Community password
    # session, with this tenant's id, this token_role floor and
    # `kind="operator"` -- `totp_gate` never mints.
    async with tenant_session(org.id) as db:
        outcome = await totp_gate(db, member_id=member_id)

    provider = get_identity_provider()

    if outcome.requires_totp_code:
        # Outcome 3: a challenge is required, whatever the member's role.
        token = provider.mint(
            tenant_id=org.id,
            subject=subject,
            role=token_role,
            kind="operator",
            scopes=["totp:challenge"],
        )
        principal = provider.verify(token)
        return PasswordSessionResponse(
            token=token,
            principal=principal,
            member_id=str(member_id),
            requires_totp_code=True,
        )

    if outcome.requires_totp_enrollment:
        # Outcome 2: a mandatory member's grace expired with nothing
        # enrolled -- refuse a full session.
        token = provider.mint(
            tenant_id=org.id,
            subject=subject,
            role=token_role,
            kind="operator",
            scopes=["totp:enroll"],
        )
        principal = provider.verify(token)
        return PasswordSessionResponse(
            token=token,
            principal=principal,
            member_id=str(member_id),
            requires_totp_enrollment=True,
        )

    # Outcome 1: a full session, unchanged from before this feature existed.
    # `totp_grace_expires_at` is non-None only for a member whose clock is
    # actually running (the frontend's nag banner); an opt-in, non-mandatory
    # member has no clock at all and gets exactly the response they always got.
    token = provider.mint(
        tenant_id=org.id,
        subject=subject,
        role=token_role,
        kind="operator",
    )
    principal = provider.verify(token)
    return PasswordSessionResponse(
        token=token,
        principal=principal,
        member_id=str(member_id),
        totp_grace_expires_at=(
            outcome.totp_grace_expires_at.isoformat()
            if outcome.totp_grace_expires_at is not None
            else None
        ),
    )


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(unguarded("logout is always available and is a no-op"))
    ],
)
async def password_logout() -> None:
    """Logout endpoint (no-op with stateless JWT).

    With JWT tokens, logout is handled client-side: the frontend simply discards
    the token. This endpoint exists for symmetry and client convenience.

    Raises: Nothing (always succeeds).
    """
    # Stateless JWT: no server-side session to revoke.
    # Client discards the token; subsequent requests without a token fail at
    # the authorization layer. Future work could implement token revocation via
    # a blacklist (see spec §8 rate limits + audit events).
    pass


# =============================================================================
# Public, token-authenticated account routes
# =============================================================================
# The three routes below carry NO bearer token, and that is the point: two of
# them are reached from a link in an inbox, and the third has to be reachable
# by somebody who cannot log in at all. What authenticates them is possession
# of an `account_verification_token` whose sha256 is stored -- or, for
# forgot-password, nothing whatsoever.
#
# They resolve the organization the same way `password_setup`/`password_login`
# do (unbound read of the singleton, then a tenant-bound session) rather than
# taking a `db: DbSession` parameter, for the reason spelled out on
# `password_setup`: `DbSession` resolves through `get_principal` -> a MANDATORY
# Bearer token, so a public route that declared one would answer 401/403 to
# every caller it exists for. `unguarded(...)` exempts the route from
# permission governance; it does nothing to a `DbSession` parameter.
#
# Design: docs/superpowers/specs/2026-08-28-account-self-service-design.md §5.4-5.6, §7.

#: How long a mailed password-reset link stays usable. Same hour as the
#: email-change link above, and for the same reason: the link IS a credential
#: while it lives, and it lives in an inbox somebody else may reach.
PASSWORD_RESET_TOKEN_TTL = dt.timedelta(hours=1)

#: The ONE sentence every failure of `/auth/email/confirm` and
#: `/auth/password/reset` answers with. Never expanded per case: "expired",
#: "already used" and "no such token" are three different pieces of
#: intelligence about somebody else's account, and a caller who can tell them
#: apart can probe for live links. A single constant rather than three equal
#: string literals so the next person to edit one edits all of them.
INVALID_LINK_MESSAGE = "This link is invalid or has expired."

#: The ONE sentence `/auth/password/forgot` answers with, always -- unknown
#: address, known address, no mail server, no organization at all, mail server
#: that refused the connection. See that endpoint's docstring.
FORGOT_PASSWORD_MESSAGE = (
    "If that address belongs to an account and this instance can send mail, "
    "a reset link is on its way."
)


class ForgotPasswordResponse(CamelModel):
    """The whole body of `POST /auth/password/forgot`.

    One constant field, because a response with anything variable in it is a
    response that can be compared -- and the entire contract of that endpoint
    is that two callers cannot tell their two cases apart.
    """

    message: str


async def _redeem_token(
    db: AsyncSession, *, tenant_id: uuid.UUID, token: str, purpose: str | tuple[str, ...]
) -> m.AccountVerificationToken:
    """Find an unused, unexpired token whose `purpose` is in `purpose` and SPEND it.

    Marking `used_at` here, rather than at the end of the caller, is what
    makes single-use structural instead of remembered: every path out of this
    function has either raised or already spent the row, so a caller cannot
    forget the second step. The write lands in the caller's transaction and
    commits with the action it authorizes (`tenant_session` commits once, on
    exit) -- so a token is never spent by an action that then rolls back, and
    an action never lands without spending its token.

    `with_for_update()` is the concurrency half, and it is not decorative. A
    plain `SELECT ... WHERE used_at IS NULL` followed by an `UPDATE` is a
    read-modify-write with an `await` in the middle: two requests carrying the
    SAME link both see NULL, both write, and both succeed -- the second one's
    UPDATE simply queues on the row lock and then applies on top. With `FOR
    UPDATE` the second request blocks on the lock, and PostgreSQL re-checks
    the WHERE clause against the row version it finds when the lock is
    released (READ COMMITTED EvalPlanQual); `used_at` is no longer NULL, the
    row drops out, and the loser gets the same generic refusal as any other
    spent link.

    `purpose` is part of the WHERE, not an assertion afterwards, which is what
    makes a `password_reset` token presented to `/auth/email/confirm`
    indistinguishable from a token that never existed -- it does not match, so
    it is not spent, and the caller is told nothing. A single string is the
    common case (one purpose authorizes one action); a tuple is for the one
    case where TWO purposes authorize the SAME action -- `password_reset` and
    `invite` both end in "set a new password from a mailed link", the only
    difference being why the link was minted, so `reset_password` accepts
    either without weakening the principle above: a token still never
    authorizes an action it was not minted for, it just so happens two mint
    reasons lead to the identical one.

    Raises `HTTPException(400, INVALID_LINK_MESSAGE)` for every failure --
    malformed, unknown, wrong purpose, expired, already spent.
    """
    purposes = (purpose,) if isinstance(purpose, str) else purpose
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = (
        await db.execute(
            select(m.AccountVerificationToken)
            .where(
                m.AccountVerificationToken.tenant_id == tenant_id,
                m.AccountVerificationToken.token_hash == token_hash,
                m.AccountVerificationToken.purpose.in_(purposes),
                m.AccountVerificationToken.used_at.is_(None),
                m.AccountVerificationToken.expires_at > dt.datetime.now(tz=dt.UTC),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, INVALID_LINK_MESSAGE)
    row.used_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()
    return row


async def _member_for_token(
    db: AsyncSession, row: m.AccountVerificationToken
) -> m.OrgMember:
    """The live member a spent token belongs to, or the same generic refusal.

    A member deleted between minting and confirming is answered exactly like a
    bad token: the link is dead either way, and "that account no longer
    exists" is a fact about somebody else the holder of a stale link has no
    business learning.
    """
    member = await db.get(m.OrgMember, row.member_id)
    if member is None or member.deleted_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, INVALID_LINK_MESSAGE)
    return member


@router.post(
    "/auth/email/confirm",
    response_model=MemberDTO,
    dependencies=[
        Depends(
            unguarded(
                "token-authenticated: the mailed link is the only credential, and holding "
                "it is the proof that the caller reads the address being moved to"
            )
        )
    ],
)
async def confirm_email_change(body: ConfirmEmailChangeRequest) -> MemberDTO:
    """Land the email change that `PUT /auth/me/email` parked (§5.4).

    The new address is read off the STORED row, never off this request: a body
    that also carried an address would let whoever intercepts a link redirect
    it, which is the one thing the confirmation exists to prevent.

    The uniqueness check runs again here rather than being trusted from when
    the link was mailed -- an hour is long enough for somebody else to claim
    the address, and `(tenant_id, subject)` is unique, so the row that lost
    that race would look like it had simply vanished. That 409 is the one
    outcome deliberately NOT folded into the generic message: by then the
    caller has already proved possession of a valid link for this account, so
    they learn nothing new, and it is the only refusal here they can act on.
    The link survives that 409 -- raising rolls the whole transaction back,
    `used_at` included -- which is what makes it actionable: the address can
    be freed and the same link clicked again inside its hour.

    The token is spent even though the caller must then sign in again with the
    new address -- their old session's `subject` claim no longer names a row,
    exactly as on the immediate-apply branch of `PUT /auth/me/email`.
    """
    async with tenant_session(None) as unbound_db:
        org = await _get_singleton_organization(unbound_db)

    async with tenant_session(org.id) as db:
        row = await _redeem_token(db, tenant_id=org.id, token=body.token, purpose="email_change")
        if row.new_email is None:
            # An `email_change` row with no target address cannot be applied
            # to anything. Unreachable while `PUT /auth/me/email` is the only
            # writer, and answered generically rather than as a 500 if it ever
            # stops being.
            raise HTTPException(status.HTTP_400_BAD_REQUEST, INVALID_LINK_MESSAGE)
        member = await _member_for_token(db, row)

        clash = (
            await db.execute(
                select(m.OrgMember.id).where(
                    m.OrgMember.tenant_id == org.id,
                    m.OrgMember.subject == row.new_email,
                    m.OrgMember.deleted_at.is_(None),
                    m.OrgMember.id != member.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Another user already signs in with that identity."
            )

        old_subject = member.subject
        member.subject = row.new_email
        member.subject_uuid = subject_uuid_for(row.new_email)
        await append_event(
            db,
            tenant_id=org.id,
            # "operator" and not a fourth actor_type word. The column is a
            # plain unconstrained `str` -- no CHECK, no enum anywhere in the
            # schema -- so the only thing keeping it meaningful is that every
            # call site in this codebase uses one of three words, and
            # `change_own_email` one function up already writes "operator" for
            # this same class of self-service change. Inventing "member" here
            # would put two words on one action in one file. That no session
            # was involved is said in `reason`, where it is readable, and
            # `principal=None` keeps `resolve_responsible` from attributing
            # the row to a caller who never authenticated.
            actor_type="operator",
            actor_id=member.id,
            category="member",
            action="member.subject_renamed",
            resource={
                "member_id": str(member.id),
                "previous_subject": old_subject,
                "subject": row.new_email,
            },
            reason="confirmed by the member via an emailed link; no session was involved",
        )
        # Built before the block exits, because exiting is what commits, and a
        # commit inside `tenant_session` unbinds `app.tenant_id` -- the seat
        # query behind this DTO would then come back empty. Same ordering as
        # every other writer in this module.
        dto = await _member_dto(db, member)
    return dto


@router.post(
    "/auth/password/forgot",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(
            unguarded(
                "public by necessity: this is the route for somebody who cannot log in, "
                "so it must be reachable before and without any token"
            )
        )
    ],
)
async def forgot_password(body: ForgotPasswordRequest) -> ForgotPasswordResponse:
    """Mail a reset link, and say the same thing either way (§5.5, §7).

    **Every** exit from this function returns `202` with
    `FORGOT_PASSWORD_MESSAGE` and nothing else -- there is no branch that
    returns a different status, a different body, or an error. That is the
    whole contract, and each of these cases has to reach it:

    * the instance has no organization yet, or somehow more than one;
    * no mail server is configured;
    * no member signs in with that address;
    * the member exists but has no password (an SSO/dev-token identity, with
      nothing for a reset to reset);
    * a link was already mailed to this member seconds ago (the cooldown);
    * everything is fine and a link really was mailed;
    * everything looked fine and the relay refused the connection --
      `deliver` never raises and its return value is deliberately ignored
      here, unlike `PUT /auth/me/email` where the caller is authenticated and
      HAS to be told their mail did not leave.

    Any of those answering differently turns this route into an oracle for
    "does this person have an account here", which is exactly what a login
    form is careful not to be. The server-side log lines inside `send_mail`
    are where an operator finds out a send failed.

    What this does NOT claim is constant time: a real address costs a member
    lookup, a token write and an SMTP round-trip that a fake one does not, and
    equalising that needs a queue this deployment does not have (§7 asks for
    an identical *response*, and that is what is enforced and tested).

    Two things bound the damage, and they are not the same thing:

    * a repeat request spends the previous link before minting a new one, so a
      burst collapses to ONE usable link rather than one per request, and a
      link somebody has already been mailed stops working;
    * `VERIFICATION_MAIL_COOLDOWN_SECONDS` bounds how many mails are SENT.
      Without it the point above still leaves anybody who knows an address
      free to drive this route in a loop and fill that inbox, unauthenticated
      -- one link at a time, but one SMTP dial per request.

    A throttled request answers with the same object as every other one, so
    the cooldown cannot itself become the oracle this endpoint exists to deny.
    """
    generic = ForgotPasswordResponse(message=FORGOT_PASSWORD_MESSAGE)
    try:
        async with tenant_session(None) as unbound_db:
            org = await _get_singleton_organization(unbound_db)
    except HTTPException:
        # Not initialized, or multi-organization. Both are real 404/409s for
        # `password_login`, which is authenticated-adjacent; here they would
        # be a free fingerprint of the deployment.
        return generic

    async with tenant_session(org.id) as db:
        if await active_smtp_credential(db, tenant_id=org.id) is None:
            return generic
        member = await _member_by_sign_in_address(db, tenant_id=org.id, email=body.email)
        if member is None or member.password_hash is None:
            return generic

        if await _mail_is_cooling_down(
            db, tenant_id=org.id, member_id=member.id, purpose="password_reset"
        ):
            # A link is already on its way to this person. Nothing is minted,
            # nothing is mailed, and the answer is the one every other exit
            # gives.
            return generic

        # Everything the send needs, read while the transaction is still open
        # and still RLS-bound -- `app.tenant_id` dies at commit, so this cannot
        # move below the block. Resolved before the token is written so a
        # broken credential leaves no row behind, exactly as before.
        smtp = await resolve_smtp_config(db, tenant_id=org.id)
        if smtp is None:
            return generic

        await db.execute(
            update(m.AccountVerificationToken)
            .where(
                m.AccountVerificationToken.tenant_id == org.id,
                m.AccountVerificationToken.member_id == member.id,
                m.AccountVerificationToken.purpose == "password_reset",
                m.AccountVerificationToken.used_at.is_(None),
            )
            .values(used_at=dt.datetime.now(tz=dt.UTC))
        )
        token = secrets.token_urlsafe(32)
        db.add(
            m.AccountVerificationToken(
                tenant_id=org.id,
                member_id=member.id,
                purpose="password_reset",
                # The plaintext is mailed once and never stored: a dump of this
                # table is then a list of spent hashes, not a list of live links.
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=dt.datetime.now(tz=dt.UTC) + PASSWORD_RESET_TOKEN_TTL,
            )
        )
        await append_event(
            db,
            tenant_id=org.id,
            actor_type="operator",
            actor_id=member.id,
            category="member",
            action="member.password_reset_requested",
            resource={"member_id": str(member.id), "subject": member.subject},
            reason="a reset link was requested for this address; nobody was authenticated",
        )
        # Read here, used after the block: touching an ORM attribute once the
        # session is closed is a lazy refresh that has neither a connection nor
        # a bound tenant left. Mailed to the address ON THE ACCOUNT, not to the
        # casing the caller typed.
        to = member.subject
    # OUTSIDE the transaction, which is the whole point of this ordering: the
    # block above has committed and handed its DB connection back to the pool,
    # so a relay that never answers now costs this request 40 seconds of its
    # own time and nothing of anybody else's. Held inside, ~15 concurrent
    # requests to this UNAUTHENTICATED route exhausted the pool for the entire
    # process. See oc8.mail.send's module docstring.
    base = get_settings().frontend_base_url.rstrip("/")
    # Return value ignored on purpose -- see the docstring. A failed send must
    # not change the answer, and it must not undo the token either: the person
    # is no worse off with an unusable row than with none, and undoing it would
    # be a second, timing-visible branch.
    await deliver(
        smtp,
        to=to,
        subject="Reset your password",
        body=(
            f"Click this link to set a new password:\n"
            f"{base}/reset-password?token={token}\n\n"
            "This link expires in 1 hour. If you didn't request this, ignore this email."
        ),
    )
    return generic


@router.post(
    "/auth/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            unguarded(
                "token-authenticated: the mailed link is the only credential a person "
                "locked out of their account can present, and it is single-use"
            )
        )
    ],
)
async def reset_password(body: ResetPasswordRequest) -> None:
    """Spend a reset link on a new password (§5.6).

    No current-password gate, because there is nobody here who knows one --
    that is the situation this route exists for. What replaces it is the
    token: mailed to the address already on the account, valid for an hour
    (a `password_reset` link) or 7 days (`members.py`'s `INVITE_TOKEN_TTL`, an
    `invite` link minted by `POST /members` for somebody created with no
    password), and spent in the same transaction as the hash it authorizes.

    Accepts either purpose -- see `_redeem_token`'s docstring for why that is
    not a widening of what a token authorizes. The frontend's
    `/reset-password?token=...` page needs no changes for this: it already
    just posts whatever token is in the URL here.

    Answers `204` and echoes nothing. Every failure is the same generic 400
    (`_redeem_token`), so a caller cannot use this to test whether a link they
    found is still live for some other reason.
    """
    async with tenant_session(None) as unbound_db:
        org = await _get_singleton_organization(unbound_db)

    async with tenant_session(org.id) as db:
        row = await _redeem_token(
            db, tenant_id=org.id, token=body.token, purpose=("password_reset", "invite")
        )
        member = await _member_for_token(db, row)
        try:
            member.password_hash = hash_password(body.new_password)
        except PasswordHashingError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Could not hash password."
            ) from exc
        await append_event(
            db,
            tenant_id=org.id,
            actor_type="operator",
            actor_id=member.id,
            category="member",
            action="member.password_reset",
            resource={"member_id": str(member.id)},
            reason="reset by the member via an emailed link; no session was involved",
        )
