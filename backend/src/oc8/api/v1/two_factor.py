"""Second factors other than the TOTP routes in `totp.py`: a code by email.

Like `totp.py`, every route here acts on the CALLER's own member row, derived
from the token, never from a path parameter -- holding the token is the
authorization. Which pending tokens reach which route is decided in exactly
one place, `deny_totp_pending_principals` (`api/deps.py`): an enrolment token
may enrol, a challenge token may only ask for and answer a login code.

A code by email is only as strong as the mailbox; it is meant for members who
cannot use an authenticator app, and the organisation's policy decides
whether it is allowed at all.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy import select, update

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.api.v1.auth import PasswordSessionResponse
from oc8.api.v1.totp import mint_full_session
from oc8.auth import Principal
from oc8.auth.email_otp import (
    CheckResult,
    CoolingDown,
    Purpose,
    check_code,
    code_mail,
    issue_code,
    spend,
)
from oc8.auth.totp import generate_backup_codes, hash_backup_code
from oc8.auth.totp_gate import member_id_for_principal
from oc8.auth.two_factor_policy import load_policy, member_methods
from oc8.db.session import tenant_session
from oc8.mail.send import active_smtp_credential, deliver, resolve_smtp_config
from oc8.schemas.base import CamelModel

router = APIRouter()

_ENROL_REASON = (
    "self-service: acts only on the caller's own member row (member id from the "
    "token, never a path parameter); reachable by a full session or a "
    "totp:enroll-scoped token, which deny_totp_pending_principals confines here"
)
_CHALLENGE_REASON = (
    "completes the caller's own login challenge (member id from the token, never "
    "a path parameter); a totp:challenge-scoped token is confined to the "
    "challenge routes by deny_totp_pending_principals, and a full session only "
    "re-mints its own session"
)


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
    db: DbSession, principal: Principal, member: m.OrgMember, purpose: Purpose
) -> EmailCodeSentResponse:
    """Issue a code, commit, then mail it with no connection checked out
    (`oc8.mail.send`'s rule). A code whose mail failed is spent at once."""
    address = _address(member)
    smtp = await resolve_smtp_config(db, tenant_id=principal.tenant_id)
    if smtp is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Outgoing mail is not configured.")
    try:
        challenge_id, code = await issue_code(
            db, tenant_id=principal.tenant_id, member_id=member.id, purpose=purpose
        )
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


async def _answer(db: DbSession, member: m.OrgMember, purpose: Purpose, code: str) -> CheckResult:
    """Check a code and commit the outcome either way: a wrong attempt must
    stay counted although the request then fails."""
    result = await check_code(db, member_id=member.id, purpose=purpose, code=code)
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
    already = (
        await db.execute(
            select(m.SecondFactor.id).where(
                m.SecondFactor.member_id == member.id, m.SecondFactor.method == "email"
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Codes by email are already turned on.")
    return await _mail_code(db, principal, member, "enrol")


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
    if await _answer(db, member, "enrol", body.code) is not CheckResult.OK:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired code.")

    # `_answer` committed; a fresh, tenant-bound session for the enrolment.
    async with tenant_session(tenant_id) as tx:
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
        cred = (
            await tx.execute(
                select(m.TotpCredential)
                .where(m.TotpCredential.member_id == member_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        codes: list[str] = []
        if cred is None or not cred.backup_codes:
            codes = generate_backup_codes()
            hashed = [{"hash": hash_backup_code(c), "used_at": None} for c in codes]
            if cred is None:
                # No TOTP secret: this row only carries the backup codes.
                tx.add(
                    m.TotpCredential(
                        tenant_id=tenant_id,
                        member_id=member_id,
                        secret_ref="",
                        enrolled_at=None,
                        backup_codes=hashed,
                    )
                )
            else:
                cred.backup_codes = hashed
        # Enrolled: the grace clock is cleared, as TOTP confirm does.
        await tx.execute(
            update(m.OrgMember)
            .where(m.OrgMember.id == member_id)
            .values(totp_grace_started_at=None)
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
    return await _mail_code(db, principal, member, "login")


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
    if await _answer(db, member, "login", body.code) is not CheckResult.OK:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired code.")
    async with tenant_session(principal.tenant_id) as tx:
        await tx.execute(
            update(m.SecondFactor)
            .where(m.SecondFactor.member_id == member_id, m.SecondFactor.method == "email")
            .values(last_used_at=dt.datetime.now(tz=dt.UTC))
        )
    return mint_full_session(principal, member_id)
