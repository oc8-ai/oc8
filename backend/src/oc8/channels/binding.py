"""Linking a messenger account to an oc8 user, and unlinking it again (§5.6).

This is the part of an approval channel that decides whether it is safe at all.
A Telegram chat id is not a user; a WhatsApp phone number is a value anyone can
put in a message. If oc8 inferred the link from either, the bot's name plus a
spoofed number would be enough to approve a refund.

So the link is never inferred. An authenticated person asks oc8 for a code, sends
that code to the bot, and the bot exchanges it. Three properties make that hold:

* the code is single-use and short-lived, so a screenshot in a chat history is
  not a standing credential;
* it is compared in constant time, because it is a bearer token for the moments
  it is alive;
* an unknown sender is answered with nothing that tells them whether an account
  exists -- an error that distinguishes "no such code" from "code already used"
  is a probe oracle.
"""

from __future__ import annotations

import datetime as dt
import hmac
import logging
import secrets
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.permissions import SEAT_PERMISSIONS
from oc8.authz.scope import APPROVAL_VIEW
from oc8.models.identity import OrgMember, OrgMemberDepartment

logger = logging.getLogger(__name__)

#: The seat roles that carry `approval:view`, i.e. the ones a fan-out may reach.
#: Derived from the vocabulary rather than spelled as `!= 'dept_viewer'` so the
#: seat table and the notification cannot drift apart.
_SEATS_THAT_MAY_BE_TOLD: frozenset[str] = frozenset(
    role for role, carried in SEAT_PERMISSIONS.items() if APPROVAL_VIEW in carried
)

#: Long enough that guessing is hopeless, short enough to type from a phone.
#: 32 bits of entropy would be guessable at a few requests a second; this is 60.
CODE_BYTES = 8

#: A code is quoted into a chat by hand. Ten minutes is long enough for someone
#: to switch apps and paste it, short enough that a code left in a chat history
#: is worthless by the time anybody scrolls back to it.
CODE_TTL = dt.timedelta(minutes=10)


class BindingError(Exception):
    """A code could not be redeemed. Deliberately one type with one message:
    telling "expired" from "already used" from "never existed" would let anyone
    with the bot's address probe for live codes."""


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def new_code() -> str:
    return secrets.token_urlsafe(CODE_BYTES)


async def issue_code(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: str,
    member_id: uuid.UUID | None = None,
) -> m.ApprovalChannelBinding:
    """Start a binding for an authenticated user. Returns the row; the CALLER
    shows `code` to that user and nobody else.

    `member_id` is the person this phone speaks for, and it is written HERE --
    at the one authenticated moment in the whole channel flow -- because the
    webhook that redeems the code carries no token and could only guess. A
    binding issued without one decides nothing and is told nothing: that is the
    state every pre-slice-1 row is in, deliberately un-grandfathered, and it is
    the safe default for any caller that has not been taught to resolve a member.
    """
    row = m.ApprovalChannelBinding(
        tenant_id=tenant_id,
        channel=channel,
        user_id=user_id,
        member_id=member_id,
        external_id=None,
        code=new_code(),
        code_expires_at=_now() + CODE_TTL,
    )
    db.add(row)
    await db.flush()
    return row


async def redeem_code(
    db: AsyncSession, *, tenant_id: uuid.UUID, channel: str, code: str, external_id: str
) -> m.ApprovalChannelBinding:
    """Turn a code somebody sent the bot into a live binding.

    Raises `BindingError` for every failure, with one message.
    """
    candidates = (
        (
            await db.execute(
                select(m.ApprovalChannelBinding).where(
                    m.ApprovalChannelBinding.tenant_id == tenant_id,
                    m.ApprovalChannelBinding.channel == channel,
                    m.ApprovalChannelBinding.external_id.is_(None),
                    m.ApprovalChannelBinding.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    # Compared in constant time against every open code rather than looked up by
    # equality: a code is a bearer token while it lives, and an indexed lookup
    # leaks its prefix through timing. The set is small by construction -- codes
    # expire in minutes and are issued by hand.
    now = _now()
    match: m.ApprovalChannelBinding | None = None
    for row in candidates:
        if row.code is None or row.code_expires_at is None:
            continue
        if hmac.compare_digest(row.code, code) and row.code_expires_at > now:
            match = row
            break
    if match is None:
        raise BindingError("that code is not valid")

    # Somebody re-binding replaces their own earlier account rather than
    # accumulating two: an approver who switches phones should not leave a live
    # binding on the old one, and nobody thinks to revoke it.
    previous = await _live_for_user(db, tenant_id=tenant_id, channel=channel, user_id=match.user_id)
    for old in previous:
        old.revoked_at = now

    match.external_id = external_id
    match.code = None
    match.code_expires_at = None
    await db.flush()
    return match


async def _live_for_user(
    db: AsyncSession, *, tenant_id: uuid.UUID, channel: str, user_id: uuid.UUID
) -> list[m.ApprovalChannelBinding]:
    return list(
        (
            await db.execute(
                select(m.ApprovalChannelBinding).where(
                    m.ApprovalChannelBinding.tenant_id == tenant_id,
                    m.ApprovalChannelBinding.channel == channel,
                    m.ApprovalChannelBinding.user_id == user_id,
                    m.ApprovalChannelBinding.external_id.is_not(None),
                    m.ApprovalChannelBinding.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def resolve(
    db: AsyncSession, *, tenant_id: uuid.UUID, channel: str, external_id: str
) -> m.ApprovalChannelBinding | None:
    """The binding an inbound message belongs to, or None.

    None means "this sender may not decide anything here". The caller must say
    nothing more than that -- see `BindingError`.
    """
    return (
        await db.execute(
            select(m.ApprovalChannelBinding).where(
                m.ApprovalChannelBinding.tenant_id == tenant_id,
                m.ApprovalChannelBinding.channel == channel,
                m.ApprovalChannelBinding.external_id == external_id,
                m.ApprovalChannelBinding.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def has_ever_been_bound(
    db: AsyncSession, *, tenant_id: uuid.UUID, channel: str, external_id: str
) -> bool:
    """Whether this messenger account has ANY binding row here at all --
    revoked ones and member-less ones included.

    Deliberately wider than `resolve`, and used for exactly one thing: deciding
    whether the bot may open its mouth at all. An account that has been through
    the link-code flow at least once already has a relationship with this
    tenant, so a refusal aimed at it discloses nothing it could not already
    infer. An account with NO row whatsoever is a stranger who merely found the
    bot's address, and answering one of those turns the bot into an
    unauthenticated outbound-message amplifier.

    It is NOT an authorization check and must never be used as one: every
    caller that acts on a message still goes through `resolve` (live, unrevoked,
    with a member behind it) first, and every refusal is still the same
    sentence.
    """
    count = await db.scalar(
        select(func.count())
        .select_from(m.ApprovalChannelBinding)
        .where(
            m.ApprovalChannelBinding.tenant_id == tenant_id,
            m.ApprovalChannelBinding.channel == channel,
            m.ApprovalChannelBinding.external_id == external_id,
        )
    )
    return bool(count)


async def recipients(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    department_id: uuid.UUID | None,
    assignee_member_id: uuid.UUID | None = None,
) -> list[m.ApprovalChannelBinding]:
    """Everyone bound to this channel who may be told about THIS department's
    approval.

    `department_id` is a required keyword rather than an optional filter, and
    that is the same forcing trick `decide_approval`'s `actor` uses: a caller
    added next year cannot fan out to a whole tenant by omission. Until this
    slice the query filtered on tenant and channel alone, so a Head of Sales who
    had bound Telegram was messaged about Engineering's held tool calls -- with
    the title and the amount in the notification, which is a disclosure before
    anybody has decided anything.

    `department_id is None` means the approval is TENANT-WIDE (the budget
    incident), and reaches only the unrestricted. It must not be read as "no
    department filter": that is the same fail-open shape as skipping an empty
    `IN ()`.

    A binding is kept only when the person behind it is still present and either
    stands in every department or holds a live seat in this one. Three things
    earn their place: the INNER join (a binding whose `member_id` is NULL names
    nobody -- that is every row from before this slice, deliberately not
    grandfathered -- and an inner join drops it without a second predicate),
    `deleted_at IS NULL` (nothing writes it yet -- offboarding is §10 item 9 --
    but the predicate is what makes that a one-line change rather than an audit
    of every fan-out), and `revoked_at IS NULL` on the seat (a revoked approver
    stops being told on the next approval rather than on the next token).

    The orphan count below is the SIGNAL for that first one. Dropping every
    pre-slice binding is deliberate, and it is also silent: on the live system it
    is the demo's Telegram binding, which simply stops receiving announcements and
    withdrawals with nothing logged and `raise_approval` succeeding. The only
    trace was a comment in migration 0046. One small indexed count per announce
    buys a line that names the row and the fix.

    `assignee_member_id` narrows the audience to that one member: an approval
    their own guardrail asked for (D3a) is theirs to answer, and telling the
    department's approvers about it would hand them a button they may not press
    and a disclosure they were never owed.
    """
    seat = (
        select(OrgMemberDepartment.id)
        .where(
            OrgMemberDepartment.member_id == OrgMember.id,
            OrgMemberDepartment.department_id == department_id,
            OrgMemberDepartment.revoked_at.is_(None),
            # Both seat roles carry `approval:view` today, so this is inert --
            # and it is written from `SEAT_PERMISSIONS` rather than assumed, so a
            # third seat role added tomorrow does not start receiving other
            # people's approvals by inheriting the shape of this query.
            OrgMemberDepartment.seat_role.in_(_SEATS_THAT_MAY_BE_TOLD),
        )
        .exists()
    )
    covers = (
        OrgMember.all_departments if department_id is None else or_(OrgMember.all_departments, seat)
    )
    if assignee_member_id is not None:
        covers = OrgMember.id == assignee_member_id

    orphans = (
        await db.scalar(
            select(func.count())
            .select_from(m.ApprovalChannelBinding)
            .where(
                m.ApprovalChannelBinding.tenant_id == tenant_id,
                m.ApprovalChannelBinding.channel == channel,
                m.ApprovalChannelBinding.external_id.is_not(None),
                m.ApprovalChannelBinding.revoked_at.is_(None),
                m.ApprovalChannelBinding.member_id.is_(None),
            )
        )
    ) or 0
    if orphans:
        logger.warning(
            "%d live %s binding(s) in tenant %s name no org_member and are being "
            "skipped: they predate migration 0046 and were deliberately not "
            "grandfathered (inventing a member per binding would collide with "
            "uq_org_member_subject_uuid the first time that human signs in). "
            "Re-issue them with POST /channels/%s/link.",
            orphans,
            channel,
            tenant_id,
            channel,
        )

    return list(
        (
            await db.execute(
                select(m.ApprovalChannelBinding)
                .join(OrgMember, OrgMember.id == m.ApprovalChannelBinding.member_id)
                .where(
                    m.ApprovalChannelBinding.tenant_id == tenant_id,
                    m.ApprovalChannelBinding.channel == channel,
                    m.ApprovalChannelBinding.external_id.is_not(None),
                    m.ApprovalChannelBinding.revoked_at.is_(None),
                    OrgMember.tenant_id == tenant_id,
                    OrgMember.deleted_at.is_(None),
                    covers,
                )
            )
        )
        .scalars()
        .all()
    )


async def revoke(
    db: AsyncSession, *, tenant_id: uuid.UUID, binding_id: uuid.UUID
) -> m.ApprovalChannelBinding | None:
    """Take an account's right to decide away. Kept as a row with `revoked_at`
    rather than deleted: which account could approve, and until when, is exactly
    the sort of question an audit asks afterwards."""
    row = await db.get(m.ApprovalChannelBinding, binding_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    row.revoked_at = _now()
    await db.flush()
    return row
