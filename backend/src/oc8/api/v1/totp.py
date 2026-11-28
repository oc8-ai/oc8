"""POST /auth/totp/enroll and /auth/totp/confirm -- the standalone 2FA design
for Community's local password login. These endpoints only know about the
narrow token and the totp_credential row.
`POST /auth/totp/verify` -- the login-time challenge completion -- also
lives here: it exchanges a `totp:challenge`-scoped token plus a valid code
(TOTP or backup) for the real session token that login was withheld
pending."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.api.v1.auth import PasswordSessionResponse
from oc8.auth import Principal, get_identity_provider
from oc8.auth.provider import mint_password_session
from oc8.auth.second_factors import audit, lock_member
from oc8.auth.totp import (
    generate_backup_codes,
    generate_secret,
    hash_backup_code,
    provisioning_uri,
    verify_backup_code,
    verify_code,
)
from oc8.auth.totp_gate import member_id_for_principal
from oc8.auth.two_factor_policy import enrolled_methods, load_policy, member_methods
from oc8.schemas.base import CamelModel
from oc8.secrets.service import resolve_secret, store_secret

#: The `kind` this file's own vault writes always use -- never "generic",
#: which is `store_secret`'s default and what `POST /secrets` (the operator
#: CRUD, secrets.py) writes unless a caller names something else. Keeping a
#: constant here (rather than a bare string literal at the one call site)
#: is what lets secrets.py import the SAME value instead of restating
#: "totp" as a second string that could quietly drift from this one.
SECRET_KIND = "totp"

router = APIRouter()

#: Neither route below names an operator permission: both are reached by
#: nothing but the bearer token itself being valid, which is deliberate --
#: an opted-in member enrolling themselves holds no special permission to
#: check, and a grace-expired admin's narrow `totp:enroll`-scoped token
#: (Task 4's `deny_totp_pending_principals`) has NO full session at all, so
#: it could satisfy no operator-permission gate even if one were added here.
#: The narrow token's containment to exactly these two paths is handled
#: structurally at the router mount (Task 4), not in this file.
_ENROLL_UNGUARDED_REASON = (
    "reachable by any authenticated caller (full session or a grace-expired "
    "admin's totp:enroll-scoped token) generating a secret; nothing is "
    "persisted here so there is no tenant data to gate"
)
_CONFIRM_UNGUARDED_REASON = (
    "reachable by any authenticated caller (full session or a grace-expired "
    "admin's totp:enroll-scoped token) completing their OWN enrollment; "
    "member_id is derived from the caller's own principal, never a path "
    "parameter, so there is no other member's data this could expose"
)
#: `deny_totp_pending_principals` (Task 4) confines a `totp:challenge`-scoped
#: token to exactly this one path -- but it only restricts WHERE such a
#: token can go, not who else may reach this route: nothing refuses an
#: already-authenticated full session that calls it too. That's fine, not a
#: hole -- holding a valid bearer token (of either kind) already IS the
#: authorization this route needs, since member_id is derived from the
#: caller's own principal, never a path parameter, so no other member's data
#: is ever exposed. A full session gains nothing from calling this: with no
#: TOTP credential enrolled it gets a 401 like anyone else, and if enrolled
#: it only re-mints its OWN session at its OWN unchanged role, or spends its
#: OWN backup codes -- self-inflicted, never a privilege escalation.
_VERIFY_UNGUARDED_REASON = (
    "the only caller that NEEDS this route holds a totp:challenge-scoped "
    "token, which deny_totp_pending_principals confines to exactly this "
    "path -- holding one IS the authorization. An already-full session is "
    "not refused here (nothing subtracts it) but gains nothing: with no "
    "enrollment it gets 401, and with one it only re-mints its own session "
    "at its own role -- member_id comes from the caller's own principal, "
    "never a path parameter"
)
#: Unlike the three routes above, this one is reachable ONLY by a full
#: session: it is absent from both of `deny_totp_pending_principals`'
#: allowlists (Task 4), so a totp:enroll- or totp:challenge-scoped token is
#: refused with a 403 before this handler ever runs -- there is no login-time
#: use case this route serves. The profile page's own read, "is the CALLING,
#: already-fully-authenticated member enrolled", needs no operator
#: permission: it is not tenant administration, it is self-inspection, and
#: member_id is derived from the caller's own principal, never a path
#: parameter, so no other member's enrollment state is ever exposed.
_STATUS_UNGUARDED_REASON = (
    "reachable only by a full session (deny_totp_pending_principals refuses "
    "a totp:enroll/totp:challenge token here -- this path is in neither of "
    "its allowlists); answers only whether the CALLING member has an "
    "enrolled credential, with member_id derived from the caller's own "
    "principal and never a path parameter, so there is no other member's "
    "state this can read"
)
#: Same full-session-only footing as `_STATUS_UNGUARDED_REASON` and for the
#: same structural reason (absent from both `deny_totp_pending_principals`
#: allowlists, so neither narrow token can reach it), but the authorization
#: story is not identical: this route WRITES, invalidating every existing
#: backup code and minting a fresh set. What makes that safe without an
#: operator permission is that it can only ever act on the caller's OWN
#: credential (member_id from principal, never a path parameter) and only
#: once one already exists (404 otherwise) -- a member regenerating their
#: own backup codes is not a privilege a role system needs to arbitrate, the
#: same way changing your own password isn't.
_REGENERATE_BACKUP_CODES_UNGUARDED_REASON = (
    "reachable only by a full session (deny_totp_pending_principals refuses "
    "a totp:enroll/totp:challenge token here -- this path is in neither of "
    "its allowlists); invalidates and reissues backup codes for the CALLING "
    "member's OWN credential only (member_id from principal, never a path "
    "parameter, and 404 if none is enrolled), so this is self-service "
    "credential rotation, not an action a role system needs to gate"
)


class TotpEnrollResponse(CamelModel):
    secret: str
    provisioning_uri: str


class TotpConfirmRequest(CamelModel):
    #: A base32 TOTP secret from pyotp is always 16-32 chars in practice; 128
    #: is a generous upper bound that still stops an arbitrary-size payload
    #: (reproduced: an unbounded field let a 100KB string reach the secret
    #: vault) rather than trying to pin the exact length pyotp happens to
    #: produce today.
    secret: str = Field(min_length=16, max_length=128)
    #: A TOTP code is 6 digits; 16 leaves slack for a future format change
    #: without accepting an arbitrarily large string into `verify_code`.
    code: str = Field(max_length=16)


class TotpConfirmResponse(CamelModel):
    backup_codes: list[str]


class TotpVerifyRequest(CamelModel):
    #: Either a 6-digit TOTP code or a 10-character backup code (see
    #: auth/totp.py's `_BACKUP_CODE_LENGTH`); 16 comfortably covers both
    #: without accepting an arbitrarily large string into `verify_code`/
    #: `verify_backup_code` -- same doctrine as `TotpConfirmRequest.code`
    #: above (F3: an unbounded field previously let an oversized payload
    #: reach the secret vault).
    code: str = Field(max_length=16)


async def _member_id_or_none(db: DbSession, principal: Principal) -> uuid.UUID | None:
    """The caller's own member row id, or None if they have none yet.

    Thin wrapper around the shared `member_id_for_principal` query (it also
    lives beside `totp_gate`, so the two stay one implementation rather than
    growing a second copy of the same `select`).
    """
    return await member_id_for_principal(db, principal)


async def _member_id_for(db: DbSession, principal: Principal) -> uuid.UUID:
    row = await _member_id_or_none(db, principal)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    return row


async def _refuse_unless_totp_allowed(db: DbSession, principal: Principal) -> None:
    """A method the policy does not allow cannot be enrolled."""
    if "totp" not in (await load_policy(db, principal.tenant_id)).methods:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your organisation does not allow an authenticator app as a second factor.",
        )


async def refuse_enrol_token_once_enrolled(
    db: AsyncSession, principal: Principal, member_id: uuid.UUID | None
) -> None:
    """An enrolment token proves the password only. Login mints one because
    the member had no allowed method to answer a challenge with; once they
    have one, the token must not add another factor -- that takes a full
    session, i.e. the method they already hold. Every enrol route checks
    this, and checks it again under the member lock where it writes."""
    if "totp:enroll" not in principal.scopes or member_id is None:
        return
    member = await db.get(m.OrgMember, member_id)
    if member is not None and await member_methods(db, member):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have a second factor. Sign in with it to add another.",
        )


async def mint_full_session(
    db: AsyncSession, principal: Principal, member_id: uuid.UUID
) -> PasswordSessionResponse:
    """Exchange a passed challenge for the real session. Every second-factor
    verify route ends here, so they mint exactly the same token."""
    provider = get_identity_provider()
    # role=principal.role, NOT re-resolved from anywhere else: the challenge
    # token already carries whatever role `password_login` (Task 7) originally
    # resolved -- MEMBER_ROLE per that function's own fail-closed doctrine,
    # or "org_admin" from the bootstrap `/auth/setup` case. This endpoint's
    # only job is converting the token's SCOPE from challenge-pending to a
    # real session; it must not also upgrade the caller's role. Re-minting
    # with the challenge token's own role claim preserves that --
    # `org_member.role_id`'s override still applies on the NEXT request
    # exactly as it always does, unaffected by anything here.
    member = await db.get(m.OrgMember, member_id)
    token = mint_password_session(
        tenant_id=principal.tenant_id,
        subject=principal.subject,
        role=principal.role,
        password_hash=member.password_hash if member is not None else None,
    )
    new_principal = provider.verify(token)
    return PasswordSessionResponse(token=token, principal=new_principal, member_id=str(member_id))


@router.post(
    "/auth/totp/enroll",
    response_model=TotpEnrollResponse,
    dependencies=[Depends(unguarded(_ENROLL_UNGUARDED_REASON))],
)
async def totp_enroll(principal: CurrentPrincipal, db: DbSession) -> TotpEnrollResponse:
    """Generates a fresh secret and its otpauth:// provisioning URI. Persists
    NOTHING -- an abandoned enrollment (secret generated, never confirmed)
    must leave no half-configured state. The client holds the secret
    plaintext from here on to render the QR code and to submit back at
    /confirm; the server never remembers it until confirm creates the row."""
    await _refuse_unless_totp_allowed(db, principal)
    await refuse_enrol_token_once_enrolled(db, principal, await _member_id_or_none(db, principal))
    secret = generate_secret()
    return TotpEnrollResponse(
        secret=secret,
        provisioning_uri=provisioning_uri(secret, account_name=principal.subject),
    )


@router.post(
    "/auth/totp/confirm",
    response_model=TotpConfirmResponse,
    dependencies=[Depends(unguarded(_CONFIRM_UNGUARDED_REASON))],
)
async def totp_confirm(
    body: TotpConfirmRequest, principal: CurrentPrincipal, db: DbSession
) -> TotpConfirmResponse:
    """Verifies the first real code against the secret the client is holding
    (proof of control -- an arbitrary secret the client wasn't given would
    just fail the code check, see the spec's own reasoning), then creates
    the totp_credential row and returns 10 backup codes ONCE."""
    await _refuse_unless_totp_allowed(db, principal)
    if not verify_code(body.secret, body.code):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid code.")

    member_id = await _member_id_for(db, principal)
    await lock_member(db, member_id)
    await refuse_enrol_token_once_enrolled(db, principal, member_id)

    # `totp_credential.member_id` is UNIQUE, so a second /confirm for an
    # already-enrolled member would otherwise reach the DB constraint as the
    # only thing stopping it -- an unhandled IntegrityError, raw 500 (the
    # vault write and the credential insert already share this one
    # transaction, so a constraint violation here rolls both back cleanly;
    # this pre-check only turns that into a clean, expected answer instead
    # of relying on the constraint as the sole guard).
    # A row without `enrolled_at` only holds the backup codes of a member
    # whose method is email; TOTP is added to it rather than refused.
    existing = (
        await db.execute(select(m.TotpCredential).where(m.TotpCredential.member_id == member_id))
    ).scalar_one_or_none()
    if existing is not None and existing.enrolled_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this member is already enrolled in TOTP")

    secret_ref = f"totp:{member_id}"
    # `kind=SECRET_KIND` ("totp"), never the `store_secret` default of
    # "generic": secrets.py's operator-facing CRUD refuses to create or
    # delete a `kind="totp"` row (and excludes it from `GET /secrets`)
    # precisely so this vault entry cannot be silently overwritten or
    # deleted through a different door than the enrollment flow that owns
    # it -- see secrets.py's own comments on `create_secret`/
    # `delete_secret_endpoint`/`list_secrets`.
    await store_secret(
        db, tenant_id=principal.tenant_id, name=secret_ref, value=body.secret, kind=SECRET_KIND
    )

    codes = generate_backup_codes()
    hashed_codes = [{"hash": hash_backup_code(c), "used_at": None} for c in codes]

    if existing is None:
        db.add(
            m.TotpCredential(
                tenant_id=principal.tenant_id,
                member_id=member_id,
                secret_ref=secret_ref,
                enrolled_at=dt.datetime.now(tz=dt.UTC),
                backup_codes=hashed_codes,
            )
        )
    else:
        # Fresh codes replace the email enrolment's: this response shows codes,
        # and showing a set that does not work would be worse than a new one.
        existing.secret_ref = secret_ref
        existing.enrolled_at = dt.datetime.now(tz=dt.UTC)
        existing.backup_codes = hashed_codes
    # Enforce the invariant models/identity.py's own docstring documents for
    # totp_grace_started_at ("NULL for anyone who has enrolled"): nothing
    # else clears it at enrollment time (Task 8's assign_role/password_setup
    # only START it), so a mandatory admin who enrolls before their deadline
    # would otherwise keep a stale non-NULL clock forever. Currently inert
    # either way -- password_login checks TotpCredential.enrolled_at before
    # ever reading the clock -- but leaving the model's own stated invariant
    # unenforced by any code is a real gap in itself, not just belt-and-braces.
    await db.execute(
        update(m.OrgMember).where(m.OrgMember.id == member_id).values(totp_grace_started_at=None)
    )
    await audit(
        db,
        tenant_id=principal.tenant_id,
        actor_id=member_id,
        action="two_factor.enrolled",
        member_id=member_id,
        method="totp",
        principal=principal,
    )
    await db.commit()
    return TotpConfirmResponse(backup_codes=codes)


@router.post(
    "/auth/totp/verify",
    response_model=PasswordSessionResponse,
    dependencies=[Depends(unguarded(_VERIFY_UNGUARDED_REASON))],
)
async def totp_verify(
    body: TotpVerifyRequest, principal: CurrentPrincipal, db: DbSession
) -> PasswordSessionResponse:
    """Exchanges a totp:challenge-scoped token plus a valid TOTP code (or an
    unused backup code) for a REAL session token -- the login-time challenge
    this route exists to complete. Tries a live TOTP code first, then falls
    back to backup codes; either satisfies the challenge.

    By the time this handler runs, `deny_totp_pending_principals` (Task 4)
    has already confined the caller to exactly this route on the strength of
    a totp:challenge-scoped token, which only `password_login` mints and
    only after the password itself checked out -- so `principal` here IS
    that already-password-verified caller's own claims, nothing further to
    re-derive.
    """
    member_id = await _member_id_for(db, principal)

    # SELECT ... FOR UPDATE: this handler's read-modify-write of
    # `backup_codes` (checking `used_at`, then writing a burned entry back)
    # is NOT safe under plain READ COMMITTED without a row lock. Without one,
    # two concurrent /verify calls presenting the SAME backup code would both
    # read `used_at: None`, both compute their own "burned" copy of the list
    # from that identical stale snapshot, and whichever UPDATE commits second
    # would silently overwrite the first's change -- the whole JSONB column
    # is replaced wholesale, not merged per-element, so this is a real lost-
    # update hazard, not a hypothetical one. Locking the row here makes the
    # second request's SELECT block until the first COMMITs, so it re-reads
    # the ALREADY-burned entry and correctly refuses it. This is what makes
    # the burn atomic, not the fact that both statements share a
    # transaction -- sharing a transaction alone does not serialize two
    # concurrent requests, each running in ITS OWN transaction, against each
    # other.
    cred = (
        await db.execute(
            select(m.TotpCredential)
            .where(m.TotpCredential.member_id == member_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    # The policy decides what may answer: a TOTP code only while TOTP is
    # allowed, a backup code only while the member holds some allowed method.
    # A challenge token minted before the policy changed gains nothing.
    member = await db.get(m.OrgMember, member_id)
    methods = await member_methods(db, member) if member is not None else []
    if cred is None or not methods:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No second factor enrolled.")

    totp_matched = False
    if "totp" in methods and cred.enrolled_at is not None:
        secret = await resolve_secret(db, tenant_id=principal.tenant_id, ref=cred.secret_ref)
        totp_matched = verify_code(secret, body.code)
    if not totp_matched:
        matched_index = None
        # A backup code is always exactly 10 chars (auth/totp.py's
        # _BACKUP_CODE_LENGTH); a wrong-length code can never match one, so
        # skip the Argon2id scan entirely rather than hashing against all 10
        # stored entries for free -- each verify_backup_code call is ~28ms of
        # blocking CPU on this event loop, and the common real case (a
        # mistyped 6-digit TOTP code) would otherwise pay the full ~280ms
        # scan on every wrong guess, held under this row's SELECT ... FOR
        # UPDATE lock the whole time.
        if len(body.code) == 10:
            for i, entry in enumerate(cred.backup_codes):
                stored_hash = entry.get("hash")
                if (
                    entry.get("used_at") is None
                    and stored_hash is not None
                    and verify_backup_code(body.code, stored_hash)
                ):
                    matched_index = i
                    break
        if matched_index is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code.")
        updated_codes = list(cred.backup_codes)
        updated_codes[matched_index] = {
            **updated_codes[matched_index],
            "used_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        }
        cred.backup_codes = updated_codes
        # else: a live TOTP code matched -- nothing to write, fall through
        # to minting below with the row lock still held (released at commit).

    await db.commit()

    return await mint_full_session(db, principal, member_id)


class TotpStatusResponse(CamelModel):
    enrolled: bool


@router.get(
    "/auth/totp/status",
    response_model=TotpStatusResponse,
    dependencies=[Depends(unguarded(_STATUS_UNGUARDED_REASON))],
)
async def totp_status(principal: CurrentPrincipal, db: DbSession) -> TotpStatusResponse:
    """Whether the CALLING member (a full session only -- deny_totp_pending_
    principals already refuses a narrow token before this route is reached)
    has a confirmed TotpCredential. The profile page's own read: toggle-on
    vs. regenerate-codes is a binary choice driven entirely by this."""
    member_id = await _member_id_for(db, principal)
    enrolled_at = (
        await db.execute(
            select(m.TotpCredential.enrolled_at).where(m.TotpCredential.member_id == member_id)
        )
    ).scalar_one_or_none()
    return TotpStatusResponse(enrolled=enrolled_at is not None)


class TotpRegenerateBackupCodesResponse(CamelModel):
    backup_codes: list[str]


@router.post(
    "/auth/totp/regenerate-backup-codes",
    response_model=TotpRegenerateBackupCodesResponse,
    dependencies=[Depends(unguarded(_REGENERATE_BACKUP_CODES_UNGUARDED_REASON))],
)
async def totp_regenerate_backup_codes(
    principal: CurrentPrincipal, db: DbSession
) -> TotpRegenerateBackupCodesResponse:
    """Invalidates every existing backup code and issues a fresh set --
    requires a FULL session (an already-enrolled member acting from their
    own profile), never reachable with a narrow token (deny_totp_pending_principals
    would refuse it, and this route is deliberately absent from that
    dependency's allowlist)."""
    member_id = await _member_id_for(db, principal)
    cred = (
        await db.execute(select(m.TotpCredential).where(m.TotpCredential.member_id == member_id))
    ).scalar_one_or_none()
    # Backup codes belong to whichever method the member uses: an email-only
    # member's row has no TOTP enrolment but holds their codes all the same.
    if cred is None or not await enrolled_methods(db, member_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No second factor enrolled.")

    codes = generate_backup_codes()
    cred.backup_codes = [{"hash": hash_backup_code(c), "used_at": None} for c in codes]
    await audit(
        db,
        tenant_id=principal.tenant_id,
        actor_id=member_id,
        action="two_factor.backup_codes_regenerated",
        member_id=member_id,
        principal=principal,
    )
    await db.commit()
    return TotpRegenerateBackupCodesResponse(backup_codes=codes)
