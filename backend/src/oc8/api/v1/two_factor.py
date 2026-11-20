"""Second factors other than the TOTP routes in `totp.py`: a code by email
and passkeys (WebAuthn, see `auth/passkey.py`).

Like `totp.py`, every route here acts on the CALLER's own member row, derived
from the token, never from a path parameter -- holding the token is the
authorization. Which pending tokens reach which route is decided in exactly
one place, `deny_totp_pending_principals` (`api/deps.py`): an enrolment token
may enrol, a challenge token may only ask for and answer a login challenge.

A code by email is only as strong as the mailbox; it is meant for members who
cannot use an authenticator app, and the organisation's policy decides
whether it is allowed at all.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import Field, StringConstraints
from sqlalchemy import select, update

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.api.v1.auth import PasswordSessionResponse
from oc8.api.v1.totp import mint_full_session, refuse_enrol_token_once_enrolled
from oc8.auth import Principal
from oc8.auth.email_otp import (
    Capped,
    CheckResult,
    CoolingDown,
    Purpose,
    check_code,
    code_mail,
    issue_code,
    spend,
)
from oc8.auth.passkey import (
    PasskeyRefused,
    RelyingParty,
    authentication_options,
    credential_id_of,
    issue_challenge,
    member_passkeys,
    registration_options,
    relying_party,
    sign_count_advances,
    take_challenge,
    verify_assertion,
    verify_registration,
)
from oc8.auth.password import verify_password
from oc8.auth.second_factors import (
    audit,
    issue_backup_codes_if_none,
    lock_member,
    remove_method,
)
from oc8.auth.totp_gate import member_id_for_principal
from oc8.auth.two_factor_policy import (
    METHODS,
    confirmed_address,
    enrollable_methods,
    enrolled_methods,
    is_org_admin,
    load_policy,
    member_methods,
    requires_member,
)
from oc8.db.session import tenant_session
from oc8.mail.send import active_smtp_credential, deliver, resolve_smtp_config
from oc8.schemas.base import CamelModel

router = APIRouter()

_ENROL_REASON = (
    "self-service: acts only on the caller's own member row (member id from the "
    "token, never a path parameter); reachable by a full session or a "
    "totp:enroll-scoped token, which deny_totp_pending_principals confines here"
)
_MANAGE_REASON = (
    "self-service: lists or removes the caller's OWN second factors (member id "
    "from the token, the path names only a method); a full session only, since "
    "neither pending token's allowlist in deny_totp_pending_principals names it, "
    "and removal re-checks the current password"
)
_CHALLENGE_REASON = (
    "completes the caller's own login challenge (member id from the token, never "
    "a path parameter); a totp:challenge-scoped token is confined to the "
    "challenge routes by deny_totp_pending_principals, and a full session only "
    "re-mints its own session"
)


_CAPPED_MESSAGE = (
    "Too many wrong codes today. Sign in with a backup code or another method, "
    "or try again tomorrow."
)


def _refuse_unless_ok(result: CheckResult) -> None:
    if result is CheckResult.CAPPED:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _CAPPED_MESSAGE)
    if result is not CheckResult.OK:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired code.")


class EmailCodeSentResponse(CamelModel):
    #: The address the code went to, so the screen can say where to look.
    sent_to: str


class EmailCodeRequest(CamelModel):
    #: Six digits; the bound only stops an oversized payload reaching Argon2.
    code: str = Field(max_length=16)


class EmailConfirmResponse(CamelModel):
    #: Shown once. Empty when the member already had backup codes.
    backup_codes: list[str]


async def _own_member(db: DbSession, principal: Principal) -> m.OrgMember:
    member_id = await member_id_for_principal(db, principal)
    member = await db.get(m.OrgMember, member_id) if member_id is not None else None
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    return member


def _address(member: m.OrgMember) -> str:
    address = member.email or member.subject
    if "@" not in address:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Your account has no email address to send a code to."
        )
    return address


async def _mail_code(
    db: DbSession, principal: Principal, member: m.OrgMember, purpose: Purpose, address: str
) -> EmailCodeSentResponse:
    """Issue a code, commit, then mail it with no connection checked out
    (`oc8.mail.send`'s rule). A code whose mail failed is spent at once."""
    smtp = await resolve_smtp_config(db, tenant_id=principal.tenant_id)
    if smtp is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Outgoing mail is not configured.")
    try:
        challenge_id, code = await issue_code(
            db, tenant_id=principal.tenant_id, member_id=member.id, purpose=purpose
        )
    except Capped as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _CAPPED_MESSAGE) from exc
    except CoolingDown as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "A code was sent less than a minute ago. Check your inbox or try again shortly.",
        ) from exc
    await db.commit()
    subject, body = code_mail(code, purpose)
    if not await deliver(smtp, to=address, subject=subject, body=body):
        # `db` committed, so its tenant binding is gone: a fresh session.
        async with tenant_session(principal.tenant_id) as cleanup:
            await spend(cleanup, challenge_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not send the code. Check the mail server settings.",
        )
    return EmailCodeSentResponse(sent_to=address)


async def _answer(
    db: DbSession, principal: Principal, member: m.OrgMember, purpose: Purpose, code: str
) -> CheckResult:
    """Check a code and commit the outcome either way: a wrong attempt must
    stay counted although the request then fails."""
    result = await check_code(db, member_id=member.id, purpose=purpose, code=code)
    if result is CheckResult.LOCKED:
        await audit(
            db,
            tenant_id=principal.tenant_id,
            actor_id=member.id,
            action="two_factor.locked",
            member_id=member.id,
            method="email",
            principal=principal,
        )
    await db.commit()
    return result


@router.post(
    "/auth/two-factor/email/enroll",
    response_model=EmailCodeSentResponse,
    dependencies=[Depends(unguarded(_ENROL_REASON))],
)
async def email_enroll(principal: CurrentPrincipal, db: DbSession) -> EmailCodeSentResponse:
    """Mail a code to the caller's sign-in address; `confirm` enrols them."""
    policy = await load_policy(db, principal.tenant_id)
    if "email" not in policy.methods:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your organisation does not allow codes by email as a second factor.",
        )
    if await active_smtp_credential(db, tenant_id=principal.tenant_id) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Outgoing mail is not configured.")
    member = await _own_member(db, principal)
    await refuse_enrol_token_once_enrolled(db, principal, member.id)
    already = (
        await db.execute(
            select(m.SecondFactor.id).where(
                m.SecondFactor.member_id == member.id, m.SecondFactor.method == "email"
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Codes by email are already turned on.")
    return await _mail_code(db, principal, member, "enrol", _address(member))


@router.post(
    "/auth/two-factor/email/confirm",
    response_model=EmailConfirmResponse,
    dependencies=[Depends(unguarded(_ENROL_REASON))],
)
async def email_confirm(
    body: EmailCodeRequest, principal: CurrentPrincipal, db: DbSession
) -> EmailConfirmResponse:
    """Enrol the email method with the code from `enroll`, and hand out
    backup codes if the member has none yet."""
    policy = await load_policy(db, principal.tenant_id)
    if "email" not in policy.methods:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your organisation does not allow codes by email as a second factor.",
        )
    member = await _own_member(db, principal)
    address = _address(member)
    tenant_id, member_id = principal.tenant_id, member.id
    await refuse_enrol_token_once_enrolled(db, principal, member_id)
    _refuse_unless_ok(await _answer(db, principal, member, "enrol", body.code))

    # `_answer` committed; a fresh, tenant-bound session for the enrolment.
    async with tenant_session(tenant_id) as tx:
        await lock_member(tx, member_id)
        await refuse_enrol_token_once_enrolled(tx, principal, member_id)
        already = (
            await tx.execute(
                select(m.SecondFactor.id).where(
                    m.SecondFactor.member_id == member_id, m.SecondFactor.method == "email"
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Codes by email are already turned on.")
        tx.add(
            m.SecondFactor(
                tenant_id=tenant_id,
                member_id=member_id,
                method="email",
                label=address,
                data={"address": address},
            )
        )
        codes = await issue_backup_codes_if_none(tx, tenant_id=tenant_id, member_id=member_id)
        # Enrolled: the grace clock is cleared, as TOTP confirm does.
        await tx.execute(
            update(m.OrgMember)
            .where(m.OrgMember.id == member_id)
            .values(totp_grace_started_at=None)
        )
        await audit(
            tx,
            tenant_id=tenant_id,
            actor_id=member_id,
            action="two_factor.enrolled",
            member_id=member_id,
            method="email",
            principal=principal,
        )
    return EmailConfirmResponse(backup_codes=codes)


async def _refuse_unless_email_answers(db: DbSession, member: m.OrgMember) -> None:
    if "email" not in await member_methods(db, member):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Codes by email are not a method you can use."
        )


@router.post(
    "/auth/two-factor/email/send",
    response_model=EmailCodeSentResponse,
    dependencies=[Depends(unguarded(_CHALLENGE_REASON))],
)
async def email_send(principal: CurrentPrincipal, db: DbSession) -> EmailCodeSentResponse:
    """Mail a login code to a member whose allowed method is email."""
    member = await _own_member(db, principal)
    await _refuse_unless_email_answers(db, member)
    # Only ever the address the factor was confirmed for; `member_methods`
    # above already refused a factor whose address no longer matches.
    data = (
        await db.execute(
            select(m.SecondFactor.data).where(
                m.SecondFactor.member_id == member.id, m.SecondFactor.method == "email"
            )
        )
    ).scalar_one()
    address = confirmed_address(data)
    if address is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Codes by email are not a method you can use."
        )
    return await _mail_code(db, principal, member, "login", address)


@router.post(
    "/auth/two-factor/email/verify",
    response_model=PasswordSessionResponse,
    dependencies=[Depends(unguarded(_CHALLENGE_REASON))],
)
async def email_verify(
    body: EmailCodeRequest, principal: CurrentPrincipal, db: DbSession
) -> PasswordSessionResponse:
    """Answer the login challenge with the mailed code; mints the full
    session exactly as TOTP verify does."""
    member = await _own_member(db, principal)
    if "email" not in await member_methods(db, member):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired code.")
    member_id = member.id
    _refuse_unless_ok(await _answer(db, principal, member, "login", body.code))
    async with tenant_session(principal.tenant_id) as tx:
        await tx.execute(
            update(m.SecondFactor)
            .where(m.SecondFactor.member_id == member_id, m.SecondFactor.method == "email")
            .values(last_used_at=dt.datetime.now(tz=dt.UTC))
        )
    return mint_full_session(principal, member_id)


class SecondFactorItem(CamelModel):
    method: str
    label: str
    #: Whether the policy still allows it; a disallowed one is kept but unusable.
    allowed: bool
    #: False when it is the last allowed method and the policy requires one.
    removable: bool


class PasskeyItem(CamelModel):
    id: str
    label: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    #: False when it is the member's last way to answer a challenge the
    #: policy requires.
    removable: bool


class SecondFactorsResponse(CamelModel):
    methods: list[SecondFactorItem]
    #: Each passkey of the `passkey` method, oldest first.
    passkeys: list[PasskeyItem]
    #: The policy's methods, less a passkey while the public URL cannot carry one.
    allowed_methods: list[str]
    #: Allowed methods the caller can enrol in right now.
    enrollable_methods: list[str]
    #: Whether the policy requires a second factor from the caller.
    required: bool
    backup_codes_left: int


class RemoveSecondFactorRequest(CamelModel):
    current_password: str = Field(min_length=1, max_length=1024)


async def _required(db: DbSession, member: m.OrgMember) -> bool:
    policy = await load_policy(db, member.tenant_id)
    if policy.requirement == "off":
        return False
    return requires_member(policy, admin=await is_org_admin(db, member))


@router.get(
    "/auth/two-factor/methods",
    response_model=SecondFactorsResponse,
    dependencies=[Depends(unguarded(_MANAGE_REASON))],
)
async def list_second_factors(principal: CurrentPrincipal, db: DbSession) -> SecondFactorsResponse:
    member = await _own_member(db, principal)
    policy = await load_policy(db, principal.tenant_id)
    enrolled = await enrolled_methods(db, member.id)
    allowed = [x for x in enrolled if x in policy.methods]
    required = await _required(db, member)
    rows = await db.execute(
        select(m.SecondFactor.method, m.SecondFactor.label).where(
            m.SecondFactor.member_id == member.id, m.SecondFactor.method != "passkey"
        )
    )
    labels = {method: label for method, label in rows}
    passkeys = await member_passkeys(db, member.id)
    last_passkey_required = required and allowed == ["passkey"] and len(passkeys) == 1
    cred = (
        await db.execute(select(m.TotpCredential).where(m.TotpCredential.member_id == member.id))
    ).scalar_one_or_none()
    left = sum(1 for c in cred.backup_codes if c.get("used_at") is None) if cred is not None else 0
    return SecondFactorsResponse(
        methods=[
            SecondFactorItem(
                method=x,
                label=labels.get(x, ""),
                allowed=x in policy.methods,
                removable=not (required and allowed == [x]),
            )
            for x in enrolled
        ],
        passkeys=[
            PasskeyItem(
                id=str(row.id),
                label=row.label,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                removable=not last_passkey_required,
            )
            for row in passkeys
        ],
        allowed_methods=list(policy.methods),
        enrollable_methods=await enrollable_methods(db, principal.tenant_id, policy),
        required=required,
        backup_codes_left=left,
    )


@router.delete(
    "/auth/two-factor/methods/{method}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(unguarded(_MANAGE_REASON))],
)
async def remove_second_factor(
    method: str, body: RemoveSecondFactorRequest, principal: CurrentPrincipal, db: DbSession
) -> Response:
    """Remove one of the caller's own methods. Refused while it is the last
    allowed one and the policy requires two-factor from the caller."""
    member = await _own_member(db, principal)
    # Locked before anything is counted: see `lock_member`.
    await lock_member(db, member.id)
    if member.password_hash is None or not verify_password(
        body.current_password, member.password_hash
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    enrolled = await enrolled_methods(db, member.id)
    if method not in METHODS or method not in enrolled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You do not use this method.")
    policy = await load_policy(db, principal.tenant_id)
    allowed = [x for x in enrolled if x in policy.methods]
    if allowed == [method] and await _required(db, member):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your organisation requires a second factor. Add another method before "
            "removing this one.",
        )
    await remove_method(db, tenant_id=principal.tenant_id, member=member, method=method)
    await audit(
        db,
        tenant_id=principal.tenant_id,
        actor_id=member.id,
        action="two_factor.removed",
        member_id=member.id,
        method=method,
        principal=principal,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- passkeys --------------------------------------------------------------------

_PASSKEY_REFUSED = "The passkey could not be verified."


class PasskeyOptionsResponse(CamelModel):
    #: `PublicKeyCredentialCreationOptions` / `...RequestOptions` as JSON,
    #: binary values base64url-encoded, for `navigator.credentials`.
    options: dict[str, Any]


#: A passkey's name: what the member calls the device it lives on.
PasskeyLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class PasskeyRegisterRequest(CamelModel):
    #: `PublicKeyCredential.toJSON()` from `navigator.credentials.create()`.
    credential: dict[str, Any]
    label: PasskeyLabel


class PasskeyRegisterResponse(CamelModel):
    id: str
    #: Shown once. Empty when the member already had backup codes.
    backup_codes: list[str]


class PasskeyAssertRequest(CamelModel):
    #: `PublicKeyCredential.toJSON()` from `navigator.credentials.get()`.
    credential: dict[str, Any]


class PasskeyRenameRequest(CamelModel):
    label: PasskeyLabel


async def _refuse_unless_passkeys_allowed(db: DbSession, principal: Principal) -> RelyingParty:
    """The relying party, when the policy allows passkeys and the public URL
    can carry one; 403 otherwise (`load_policy` drops an unusable passkey)."""
    rp = relying_party()
    policy = await load_policy(db, principal.tenant_id)
    if rp is None or "passkey" not in policy.methods:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Passkeys are not available as a second factor in your organisation.",
        )
    return rp


@router.post(
    "/auth/two-factor/passkey/register/options",
    response_model=PasskeyOptionsResponse,
    dependencies=[Depends(unguarded(_ENROL_REASON))],
)
async def passkey_register_options(
    principal: CurrentPrincipal, db: DbSession
) -> PasskeyOptionsResponse:
    """Start registering a passkey: a fresh challenge, and the member's
    existing passkeys to exclude."""
    rp = await _refuse_unless_passkeys_allowed(db, principal)
    member = await _own_member(db, principal)
    await refuse_enrol_token_once_enrolled(db, principal, member.id)
    challenge = await issue_challenge(
        db, tenant_id=principal.tenant_id, member_id=member.id, purpose="register"
    )
    options = registration_options(
        rp, member=member, challenge=challenge, existing=await member_passkeys(db, member.id)
    )
    await db.commit()
    return PasskeyOptionsResponse(options=options)


@router.post(
    "/auth/two-factor/passkey/register/verify",
    response_model=PasskeyRegisterResponse,
    dependencies=[Depends(unguarded(_ENROL_REASON))],
)
async def passkey_register_verify(
    body: PasskeyRegisterRequest, principal: CurrentPrincipal, db: DbSession
) -> PasskeyRegisterResponse:
    """Register the passkey the browser created for the challenge from
    `options`, and hand out backup codes if the member has none yet."""
    rp = await _refuse_unless_passkeys_allowed(db, principal)
    member = await _own_member(db, principal)
    tenant_id, member_id = principal.tenant_id, member.id
    await refuse_enrol_token_once_enrolled(db, principal, member_id)
    challenge = await take_challenge(db, member_id=member_id, purpose="register")
    # Spent whatever the answer: committed before it is checked.
    await db.commit()
    if challenge is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _PASSKEY_REFUSED)
    try:
        data = verify_registration(rp, challenge=challenge, credential=body.credential)
    except PasskeyRefused as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _PASSKEY_REFUSED) from exc

    async with tenant_session(tenant_id) as tx:
        await lock_member(tx, member_id)
        await refuse_enrol_token_once_enrolled(tx, principal, member_id)
        taken = (
            await tx.execute(
                select(m.SecondFactor.id).where(
                    m.SecondFactor.method == "passkey",
                    m.SecondFactor.data["credential_id"].astext == data["credential_id"],
                )
            )
        ).first()
        if taken is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This passkey is already registered.")
        factor = m.SecondFactor(
            tenant_id=tenant_id,
            member_id=member_id,
            method="passkey",
            label=body.label,
            data=data,
        )
        tx.add(factor)
        await tx.flush()
        codes = await issue_backup_codes_if_none(tx, tenant_id=tenant_id, member_id=member_id)
        # Enrolled: the grace clock is cleared, as TOTP confirm does.
        await tx.execute(
            update(m.OrgMember)
            .where(m.OrgMember.id == member_id)
            .values(totp_grace_started_at=None)
        )
        await audit(
            tx,
            tenant_id=tenant_id,
            actor_id=member_id,
            action="two_factor.enrolled",
            member_id=member_id,
            method="passkey",
            principal=principal,
        )
        factor_id = factor.id
    return PasskeyRegisterResponse(id=str(factor_id), backup_codes=codes)


@router.post(
    "/auth/two-factor/passkey/authenticate/options",
    response_model=PasskeyOptionsResponse,
    dependencies=[Depends(unguarded(_CHALLENGE_REASON))],
)
async def passkey_authenticate_options(
    principal: CurrentPrincipal, db: DbSession
) -> PasskeyOptionsResponse:
    """A sign-in challenge for a member whose allowed method is a passkey;
    only their own passkeys may answer it."""
    member = await _own_member(db, principal)
    rp = relying_party()
    if rp is None or "passkey" not in await member_methods(db, member):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Passkeys are not a method you can use.")
    challenge = await issue_challenge(
        db, tenant_id=principal.tenant_id, member_id=member.id, purpose="login"
    )
    options = authentication_options(
        rp, challenge=challenge, passkeys=await member_passkeys(db, member.id)
    )
    await db.commit()
    return PasskeyOptionsResponse(options=options)


@router.post(
    "/auth/two-factor/passkey/authenticate/verify",
    response_model=PasswordSessionResponse,
    dependencies=[Depends(unguarded(_CHALLENGE_REASON))],
)
async def passkey_authenticate_verify(
    body: PasskeyAssertRequest, principal: CurrentPrincipal, db: DbSession
) -> PasswordSessionResponse:
    """Answer the login challenge with a passkey; mints the full session
    exactly as TOTP verify does."""
    member = await _own_member(db, principal)
    member_id = member.id
    rp = relying_party()
    if rp is None or "passkey" not in await member_methods(db, member):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _PASSKEY_REFUSED)
    challenge = await take_challenge(db, member_id=member_id, purpose="login")
    await db.commit()
    if challenge is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _PASSKEY_REFUSED)
    credential_id = credential_id_of(body.credential)

    async with tenant_session(principal.tenant_id) as tx:
        # Only the caller's own passkey, locked: two answers must not both
        # move the sign count from the same value.
        factor = (
            (
                await tx.execute(
                    select(m.SecondFactor)
                    .where(
                        m.SecondFactor.member_id == member_id,
                        m.SecondFactor.method == "passkey",
                        m.SecondFactor.data["credential_id"].astext == credential_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if credential_id is not None
            else None
        )
        if factor is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _PASSKEY_REFUSED)
        stored = int(factor.data.get("sign_count") or 0)
        try:
            new_count = verify_assertion(
                rp, challenge=challenge, credential=body.credential, data=factor.data
            )
        except PasskeyRefused as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _PASSKEY_REFUSED) from exc
        if not sign_count_advances(stored, new_count):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _PASSKEY_REFUSED)
        factor.data = {**factor.data, "sign_count": new_count}
        factor.last_used_at = dt.datetime.now(tz=dt.UTC)
    return mint_full_session(principal, member_id)


async def _last_required_passkey(db: DbSession, member: m.OrgMember) -> bool:
    """Whether the member's single passkey is their only allowed method while
    the policy requires one: then it cannot be removed."""
    policy = await load_policy(db, member.tenant_id)
    allowed = [x for x in await enrolled_methods(db, member.id) if x in policy.methods]
    return (
        allowed == ["passkey"]
        and len(await member_passkeys(db, member.id)) == 1
        and await _required(db, member)
    )


async def _own_passkey(db: DbSession, member_id: uuid.UUID, factor_id: uuid.UUID) -> m.SecondFactor:
    factor = (
        await db.execute(
            select(m.SecondFactor).where(
                m.SecondFactor.id == factor_id,
                m.SecondFactor.member_id == member_id,
                m.SecondFactor.method == "passkey",
            )
        )
    ).scalar_one_or_none()
    if factor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You have no such passkey.")
    return factor


@router.patch(
    "/auth/two-factor/passkey/{factor_id}",
    response_model=PasskeyItem,
    dependencies=[Depends(unguarded(_MANAGE_REASON))],
)
async def rename_passkey(
    factor_id: uuid.UUID, body: PasskeyRenameRequest, principal: CurrentPrincipal, db: DbSession
) -> PasskeyItem:
    """Rename one of the caller's own passkeys."""
    member = await _own_member(db, principal)
    factor = await _own_passkey(db, member.id, factor_id)
    factor.label = body.label
    await db.flush()
    item = PasskeyItem(
        id=str(factor.id),
        label=factor.label,
        created_at=factor.created_at,
        last_used_at=factor.last_used_at,
        removable=not await _last_required_passkey(db, member),
    )
    await db.commit()
    return item


@router.delete(
    "/auth/two-factor/passkey/{factor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(unguarded(_MANAGE_REASON))],
)
async def remove_passkey(
    factor_id: uuid.UUID,
    body: RemoveSecondFactorRequest,
    principal: CurrentPrincipal,
    db: DbSession,
) -> Response:
    """Remove one of the caller's own passkeys. Refused while it is their
    last way to answer a challenge the policy requires."""
    member = await _own_member(db, principal)
    await lock_member(db, member.id)
    if member.password_hash is None or not verify_password(
        body.current_password, member.password_hash
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    await _own_passkey(db, member.id, factor_id)
    if await _last_required_passkey(db, member):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your organisation requires a second factor. Add another passkey or method before "
            "removing this one.",
        )
    await remove_method(
        db, tenant_id=principal.tenant_id, member=member, method="passkey", factor_id=factor_id
    )
    await audit(
        db,
        tenant_id=principal.tenant_id,
        actor_id=member.id,
        action="two_factor.removed",
        member_id=member.id,
        method="passkey",
        principal=principal,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
