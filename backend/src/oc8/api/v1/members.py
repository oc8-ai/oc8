"""Who this tenant knows, and where each of them is allowed to stand (§5).

`member:manage` is `org_admin`-only. Deliberately in no seat vocabulary and
deliberately not in `_DEPT_MANAGER`, which otherwise carries nine `:manage`
grants: a Head of Sales who can enrol himself in Engineering is the department
boundary in a different coat, and it would arrive through the very screen this
slice exists to give him.

These routes use `require_permission`, not `require_departmental`. That is
the point -- no seat grants authority over seats, so there is nothing
departmental to resolve, and a gate that admitted "somebody with a seat
somewhere" would be exactly the hole above.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1._serializers import member_to_dto
from oc8.audit import append_event
from oc8.auth import Principal
from oc8.auth.password import PasswordHashingError, hash_password
from oc8.authz.permissions import MANAGE, MEMBER, VIEW, perm
from oc8.authz.scope import scope_for_principal, subject_uuid_for
from oc8.config import get_settings
from oc8.mail.send import SmtpConfig, deliver, resolve_smtp_config
from oc8.schemas.dto import MemberDTO, MemberPasswordResetDTO
from oc8.schemas.paging import Page
from oc8.schemas.requests import (
    CreateMemberRequest,
    GrantSeatRequest,
    RenameMemberSubjectRequest,
    SetMemberPasswordRequest,
)
from oc8.workspace.members import (
    MemberRow,
    UnknownSeatRole,
    delete_member,
    get_member,
    grant_seat,
    list_members,
    revoke_seat,
    role_names_for,
    seats_for,
    upsert_member,
)

router = APIRouter()

#: How long an invite link minted by `POST /members` stays usable. Long
#: compared to `PASSWORD_RESET_TOKEN_TTL` (auth.py, 1 hour) on purpose: a
#: password-reset link answers "I am locked out right now", which is urgent;
#: an invite answers "somebody set up my account before I ever needed it",
#: which is not -- the person it is mailed to may not open that inbox for
#: days. 7 days balances that against the same window a stale, forgotten
#: link stays a live credential for.
INVITE_TOKEN_TTL = dt.timedelta(days=7)
#: Duplicated from `auth.PASSWORD_RESET_TOKEN_TTL` rather than imported:
#: `auth.py` already imports `_member_dto` from this module.
PASSWORD_RESET_TOKEN_TTL = dt.timedelta(hours=1)


async def _spend_unused_tokens(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    purpose: str | None = None,
) -> None:
    """Mark unused verification tokens spent. `purpose=None` spends every one."""
    stmt = (
        update(m.AccountVerificationToken)
        .where(
            m.AccountVerificationToken.tenant_id == tenant_id,
            m.AccountVerificationToken.member_id == member_id,
            m.AccountVerificationToken.used_at.is_(None),
        )
        .values(used_at=dt.datetime.now(tz=dt.UTC))
    )
    if purpose is not None:
        stmt = stmt.where(m.AccountVerificationToken.purpose == purpose)
    await db.execute(stmt)


async def _mint_access_token(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member: m.OrgMember,
    purpose: str,
    ttl: dt.timedelta,
) -> str:
    """Spend unused tokens of this purpose, mint a new one, return the link.

    The plaintext is mailed (or handed back once) and never stored -- same
    discipline as every other verification token in this table.
    """
    await _spend_unused_tokens(db, tenant_id=tenant_id, member_id=member.id, purpose=purpose)
    token = secrets.token_urlsafe(32)
    db.add(
        m.AccountVerificationToken(
            tenant_id=tenant_id,
            member_id=member.id,
            purpose=purpose,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=dt.datetime.now(tz=dt.UTC) + ttl,
        )
    )
    return f"{get_settings().frontend_base_url.rstrip('/')}/reset-password?token={token}"


async def _acting_member_id(db: AsyncSession, principal: Principal) -> uuid.UUID | None:
    """The administrator's own `org_member.id`, for `granted_by` and `actor_id`.

    `None` rather than an exception for a principal that cannot stand in a
    department (a plugin token): this is attribution, and refusing the whole act
    because the actor has no row would make an audit column a prerequisite for the
    thing it describes. `append_event` still names them through `principal`.
    """
    try:
        member, _scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError:
        return None
    return member.id if member is not None else None


async def _member_dto(db: AsyncSession, member: m.OrgMember) -> MemberDTO:
    """The person as the screen sees them, with their live seats re-read.

    Built BEFORE the caller commits, deliberately: a commit inside
    `tenant_session` unbinds `app.tenant_id`, so a seat query issued afterwards
    would come back empty and the response would say the person holds nothing.
    """
    names = await role_names_for(db, tenant_id=member.tenant_id, role_ids=[member.role_id])
    return member_to_dto(
        MemberRow(
            id=member.id,
            subject=member.subject,
            display_name=member.display_name,
            all_departments=member.all_departments,
            first_seen_at=member.created_at,
            seats=tuple(await seats_for(db, member.id)),
            role_id=member.role_id,
            role_name="" if member.role_id is None else names.get(member.role_id, ""),
        )
    )


@router.get(
    "/members",
    response_model=Page[MemberDTO],
    dependencies=[Depends(require_permission(perm(MEMBER, VIEW)))],
)
async def list_members_route(
    db: DbSession,
    principal: CurrentPrincipal,
    search: str | None = None,
    group_by: str | None = Query(None, alias="groupBy"),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[MemberDTO]:
    """Everybody the tenant has seen, with their LIVE seats.

    Revoked seats are not listed and are not deleted either -- they keep their
    row so an audit can answer "who could approve this, and until when".

    Widened to the Design System Consistency plan's uniform search/filter/
    group/pagination contract (spec §1.1) -- `Page[MemberDTO]` instead of a
    bare list, matching Tasks 3-8's other list-query routes. Soft-deleted
    members (`deleted_at` set by `DELETE /members/{id}`) are omitted: the
    list is who can still sign in, not the audit of who ever could.
    """
    rows, total = await list_members(
        db,
        tenant_id=principal.tenant_id,
        search=search,
        group_by=group_by,
        limit=limit,
        offset=offset,
    )
    return Page(items=[member_to_dto(r) for r in rows], total_count=total)


@router.post(
    "/members",
    response_model=MemberDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(MEMBER, MANAGE)))],
)
async def create_member(
    body: CreateMemberRequest,
    db: DbSession,
    principal: CurrentPrincipal,
    response: Response,
) -> MemberDTO:
    """Enrol a person by their token subject, or adopt the row they already have.

    An upsert on `(tenant_id, subject)` and not an insert, because the gate mints
    a row on somebody's first request: an administrator enrolling a colleague who
    signed in this morning would otherwise get a unique-violation 500 for doing
    the obvious thing. The status code says which of the two happened -- 201 for a
    row this request created, 200 for one it merged into -- so a client that
    branches on it is not told a lie.

    `allDepartments` is tri-state and this is the ONLY writer of it in either
    direction: omitted leaves it alone, `true` grants company-wide view and
    decide, `false` takes it back. It only ever widened until now, so an
    administrator revoking it was answered 200 while the flag stayed set -- and
    the flag outlives the role that earned it, so a person demoted elsewhere
    kept the authority to sign off every approval in the company.

    No password AND no way to ever get one used to leave a member exactly
    that: created, and permanently locked out. When `body.password` is
    omitted, this is now also the ONE writer of an `invite` verification
    token -- mailed (or, with no mail server configured, handed back in
    `inviteLink` for an administrator to share by hand) so that person can set
    their own password at `/reset-password?token=...`, the same page a
    forgot-password link already lands on.

    Gated on `member.password_hash is None` rather than on `created`, so
    re-inviting an existing passwordless member (one created before this
    existed, or whose first invite link expired unused) works through this
    same door -- and on `not get_settings().is_dev`, because in dev mode local
    passwords are meaningless (the dev-login/dev-tenants flow is how everybody
    signs in there) and `GET /auth/config` reports that same flag as `mode`.
    """
    member, created = await upsert_member(
        db,
        tenant_id=principal.tenant_id,
        subject=body.subject,
        display_name=body.display_name,
        all_departments=body.all_departments,
    )
    invite_link: str | None = None
    invite_smtp: SmtpConfig | None = None
    invite_recipient: str | None = None
    if body.password is not None:
        try:
            member.password_hash = hash_password(body.password)
        except PasswordHashingError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Could not hash password."
            ) from exc
        await append_event(
            db,
            tenant_id=principal.tenant_id,
            actor_type="operator",
            actor_id=await _acting_member_id(db, principal),
            category="member",
            action="member.password_set",
            resource={"member_id": str(member.id), "subject": member.subject},
            reason="password set through POST /members",
            principal=principal,
        )
    elif not get_settings().is_dev and member.password_hash is None:
        # A re-invite (this same branch, reached again for a subject whose
        # first link leaked, went to the wrong inbox, or simply expired
        # unused) must not leave that earlier link live for the rest of its
        # 7-day `INVITE_TOKEN_TTL` -- `_mint_access_token` spends every prior
        # unused `invite` row before writing the new one.
        invite_link = await _mint_access_token(
            db,
            tenant_id=principal.tenant_id,
            member=member,
            purpose="invite",
            ttl=INVITE_TOKEN_TTL,
        )
        # Resolved here, inside the still-open, still tenant-bound transaction
        # -- RLS binds `app.tenant_id` transaction-locally, and it dies at
        # commit (see oc8.mail.send's module docstring). `member.subject` is
        # read now for the same reason: a lazy refresh after commit has
        # neither a connection nor a bound tenant left.
        invite_smtp = await resolve_smtp_config(db, tenant_id=principal.tenant_id)
        invite_recipient = member.subject
        await append_event(
            db,
            tenant_id=principal.tenant_id,
            actor_type="operator",
            actor_id=await _acting_member_id(db, principal),
            category="member",
            action="member.invited",
            resource={"member_id": str(member.id), "subject": member.subject},
            reason="invite link minted through POST /members (no password given)",
            principal=principal,
        )
    if body.all_departments is not None:
        # Audited on every request that ASSERTS it, in either direction, and not
        # only on the one that changed the value. `all_departments` is the only
        # unrestricted term the messenger door can read -- a Telegram message
        # carries no token -- so asserting it is an act whatever the previous
        # value was, and "when did this stop being true" is the question an audit
        # asks about a revocation.
        granted = body.all_departments
        action = "member.all_departments_granted" if granted else "member.all_departments_revoked"
        await append_event(
            db,
            tenant_id=principal.tenant_id,
            actor_type="operator",
            actor_id=await _acting_member_id(db, principal),
            category="member",
            action=action,
            resource={"member_id": str(member.id), "subject": member.subject},
            reason=("granted" if granted else "revoked") + " through POST /members",
            principal=principal,
        )
    dto = await _member_dto(db, member)
    await db.commit()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK

    # OUTSIDE the transaction, which just committed and handed its DB
    # connection back to the pool -- same ordering, and the same reason, as
    # `forgot_password`: `deliver` can block for ~40 seconds on a bad relay,
    # and this route is authenticated (an administrator's request), so
    # holding a pooled connection for that long is a cost every other request
    # on the process would pay. Unlike `forgot_password`, this endpoint has no
    # enumeration risk to protect -- the caller already knows this member
    # exists, they just created it -- so a failed or skipped send is not
    # hidden: `inviteSent` says so, and `inviteLink` is always returned
    # alongside it as the fallback an administrator can copy and share by
    # hand. Either way, the member itself is already created and committed;
    # a mail failure here never fails this request.
    invite_sent = False
    if invite_smtp is not None and invite_recipient is not None:
        invite_sent = await deliver(
            invite_smtp,
            to=invite_recipient,
            subject="You've been invited",
            body=(
                "An administrator created an account for you.\n"
                f"Click this link to set your password:\n{invite_link}\n\n"
                f"This link expires in {INVITE_TOKEN_TTL.days} days."
            ),
        )
    dto.invite_link = invite_link
    dto.invite_sent = invite_sent
    return dto


@router.put(
    "/members/{member_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(MEMBER, MANAGE)))],
)
async def set_member_password(
    member_id: uuid.UUID,
    body: SetMemberPasswordRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> None:
    """Set or reset a member's local password (Argon2id), administrator-only.

    `POST /auth/login` checks `password_hash` alone, so this works in any
    deployment. Never returns the hash or echoes the plaintext: a 204 says
    only that it happened, the way a "password changed" screen should.
    """
    member = await get_member(db, tenant_id=principal.tenant_id, member_id=member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    try:
        member.password_hash = hash_password(body.password)
    except PasswordHashingError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Could not hash password."
        ) from exc
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=await _acting_member_id(db, principal),
        category="member",
        action="member.password_reset",
        resource={"member_id": str(member.id), "subject": member.subject},
        reason="password reset by an administrator",
        principal=principal,
    )
    await db.commit()


@router.put(
    "/members/{member_id}/subject",
    response_model=MemberDTO,
    dependencies=[Depends(require_permission(perm(MEMBER, MANAGE)))],
)
async def rename_member_subject(
    member_id: uuid.UUID,
    body: RenameMemberSubjectRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> MemberDTO:
    """Change a member's sign-in identity -- their email, in local-password mode.

    `subject_uuid` is a deterministic function of `subject` (see
    `authz.scope.subject_uuid_for`), so both columns are rewritten together;
    leaving the old `subject_uuid` behind would desync the row from the very
    identity check the rename is supposed to update. Refused with a 409, not
    a silent adoption, if another live member in this tenant already holds
    the target subject -- `(tenant_id, subject)` is unique, and the row that
    lost the race would otherwise look like it simply vanished.
    """
    member = await get_member(db, tenant_id=principal.tenant_id, member_id=member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    new_subject = body.subject.strip()
    if new_subject != member.subject:
        clash = (
            await db.execute(
                select(m.OrgMember.id).where(
                    m.OrgMember.tenant_id == principal.tenant_id,
                    m.OrgMember.subject == new_subject,
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
        member.subject = new_subject
        member.subject_uuid = subject_uuid_for(new_subject)
        await append_event(
            db,
            tenant_id=principal.tenant_id,
            actor_type="operator",
            actor_id=await _acting_member_id(db, principal),
            category="member",
            action="member.subject_renamed",
            resource={
                "member_id": str(member.id),
                "previous_subject": old_subject,
                "subject": new_subject,
            },
            reason="sign-in identity changed by an administrator",
            principal=principal,
        )

    dto = await _member_dto(db, member)
    await db.commit()
    return dto


@router.put(
    "/members/{member_id}/departments/{department_id}",
    response_model=MemberDTO,
    dependencies=[Depends(require_permission(perm(MEMBER, MANAGE)))],
)
async def grant_seat_route(
    member_id: uuid.UUID,
    department_id: uuid.UUID,
    body: GrantSeatRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> MemberDTO:
    """Seat somebody in one department, at one of exactly two seat roles.

    The department's existence is checked before the seat is written. There is no
    foreign key on `org_member_department.department_id` -- the seat outlives an
    archived department on purpose -- so without this check a typo produces a seat
    in a department that does not exist: invisible on every screen, matching no
    approval, and not diagnosable from the row itself.
    """
    member = await get_member(db, tenant_id=principal.tenant_id, member_id=member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    department = await db.get(m.Department, department_id)
    if department is None or department.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")

    granted_by = await _acting_member_id(db, principal)
    try:
        seat, changed = await grant_seat(
            db,
            tenant_id=principal.tenant_id,
            member_id=member.id,
            department_id=department_id,
            seat_role=body.seat_role,
            agent_manage=body.agent_manage,
            granted_by=granted_by,
        )
    except UnknownSeatRole as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    if changed:
        await append_event(
            db,
            tenant_id=principal.tenant_id,
            actor_type="operator",
            actor_id=granted_by,
            category="member",
            action="member.seat_granted",
            resource={
                "member_id": str(member.id),
                "subject": member.subject,
                "department_id": str(department_id),
                "department": department.name,
                "seat_role": seat.seat_role,
                # Always named, not only on a toggle-only change: an admin
                # reading the log should see current state regardless of what
                # changed this row.
                "agent_manage": seat.agent_manage,
            },
            principal=principal,
        )
    dto = await _member_dto(db, member)
    await db.commit()
    return dto


@router.delete(
    "/members/{member_id}/departments/{department_id}",
    response_model=MemberDTO,
    dependencies=[Depends(require_permission(perm(MEMBER, MANAGE)))],
)
async def revoke_seat_route(
    member_id: uuid.UUID,
    department_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> MemberDTO:
    """Take a seat away. Effective on that person's NEXT request.

    No token refresh, no TTL, no logout -- a seat is read from this row on every
    request, which is the whole reason it is not in the JWT. The row is kept and
    gains a `revoked_at`, so "who could approve this, and until when" stays
    answerable afterwards.
    """
    member = await get_member(db, tenant_id=principal.tenant_id, member_id=member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    seat = await revoke_seat(
        db, tenant_id=principal.tenant_id, member_id=member.id, department_id=department_id
    )
    if seat is None:
        # Said out loud rather than answered 204. A revoke that silently succeeds
        # against a seat nobody holds is how an administrator walks away believing
        # they took an authority away that is still held.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no live seat in that department")

    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=await _acting_member_id(db, principal),
        category="member",
        action="member.seat_revoked",
        resource={
            "member_id": str(member.id),
            "subject": member.subject,
            "department_id": str(department_id),
            "seat_role": seat.seat_role,
            "granted_at": seat.created_at.isoformat() if seat.created_at else None,
        },
        principal=principal,
    )
    dto = await _member_dto(db, member)
    await db.commit()
    return dto


@router.post(
    "/members/{member_id}/password-reset",
    response_model=MemberPasswordResetDTO,
    dependencies=[Depends(require_permission(perm(MEMBER, MANAGE)))],
)
async def mint_member_password_reset(
    member_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> MemberPasswordResetDTO:
    """Mint a fresh set-password link for an existing member, and try to email it.

    The user-detail screen's counterpart to the invite `POST /members` mints
    on create: an administrator looking at somebody who already exists had
    no door to mint a new link or re-trigger the mail. Passwordless members
    get a 7-day `invite` (they still have a password to SET); members who
    already have a password get a 1-hour `password_reset`. Either way the
    previous unused token of that purpose is spent first, and the link is
    always returned so a missing mail server is not a dead end.
    """
    member = await get_member(db, tenant_id=principal.tenant_id, member_id=member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    if member.password_hash is None:
        purpose = "invite"
        ttl = INVITE_TOKEN_TTL
        action = "member.invited"
        reason = "invite link re-minted through POST /members/{id}/password-reset"
        mail_subject = "You've been invited"
        mail_body_prefix = (
            "An administrator created an account for you.\nClick this link to set your password:"
        )
        mail_expiry = f"This link expires in {INVITE_TOKEN_TTL.days} days."
    else:
        purpose = "password_reset"
        ttl = PASSWORD_RESET_TOKEN_TTL
        action = "member.password_reset_requested"
        reason = "reset link minted by an administrator"
        mail_subject = "Reset your password"
        mail_body_prefix = "An administrator sent you a link to set a new password:"
        mail_expiry = "This link expires in 1 hour."

    reset_link = await _mint_access_token(
        db,
        tenant_id=principal.tenant_id,
        member=member,
        purpose=purpose,
        ttl=ttl,
    )
    smtp = await resolve_smtp_config(db, tenant_id=principal.tenant_id)
    recipient = member.subject
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=await _acting_member_id(db, principal),
        category="member",
        action=action,
        resource={"member_id": str(member.id), "subject": member.subject},
        reason=reason,
        principal=principal,
    )
    await db.commit()

    reset_sent = False
    if smtp is not None:
        reset_sent = await deliver(
            smtp,
            to=recipient,
            subject=mail_subject,
            body=f"{mail_body_prefix}\n{reset_link}\n\n{mail_expiry}",
        )
    return MemberPasswordResetDTO(reset_link=reset_link, reset_sent=reset_sent)


@router.delete(
    "/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(MEMBER, MANAGE)))],
)
async def delete_member_route(
    member_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> None:
    """Soft-delete a person so they disappear from the users list and cannot sign in.

    Refuses the caller's own row: deleting yourself is the lockout
    `PUT /members/{id}/role` already refuses, by a different verb. Unused
    invite and reset links are spent in the same transaction so a leftover
    mailed token cannot resurrect access after the row is gone.
    """
    member = await get_member(db, tenant_id=principal.tenant_id, member_id=member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    if member.subject == principal.subject:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You cannot delete yourself — that would lock the only door back.",
        )

    gone = await delete_member(db, tenant_id=principal.tenant_id, member_id=member.id)
    if gone is None:  # pragma: no cover - get_member already 404'd
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    await _spend_unused_tokens(db, tenant_id=principal.tenant_id, member_id=member.id)
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=await _acting_member_id(db, principal),
        category="member",
        action="member.deleted",
        resource={"member_id": str(member.id), "subject": member.subject},
        reason="offboarded by an administrator",
        principal=principal,
    )
    await db.commit()
