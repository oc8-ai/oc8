"""Six-digit codes mailed to a member as a second factor.

A code is valid for ten minutes, accepted once, stored Argon2id-hashed and
refused after five wrong attempts; a new one can be asked for once a minute
and replaces the previous one. Twenty wrong codes in a rolling day stop the
method for that member until the day has passed. The plaintext exists only in the return value
of `issue_code`, which the caller mails and forgets.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from enum import Enum
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth.password import hash_password, verify_password

Purpose = Literal["enrol", "login"]

CODE_TTL = dt.timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_SECONDS = 60
#: Wrong codes a member may enter across all their codes in a rolling day.
DAILY_FAILED_ATTEMPTS = 20
DAILY_WINDOW = dt.timedelta(hours=24)


class CoolingDown(Exception):
    """A code of this purpose was issued less than a minute ago."""


class Capped(Exception):
    """The member entered too many wrong codes in the last day."""


class CheckResult(Enum):
    OK = "ok"
    REFUSED = "refused"
    #: Refused, and this attempt was the fifth wrong one for this code, or the
    #: one that reached the daily cap: audited once, here.
    LOCKED = "locked"
    #: Refused without looking: the daily cap was already reached.
    CAPPED = "capped"


async def failed_attempts_today(db: AsyncSession, member_id: uuid.UUID) -> int:
    """Wrong codes the member entered in the rolling day, across every code.

    Five attempts per code alone would allow five guesses a minute for ever,
    since a new code can be asked for every minute.
    """
    since = dt.datetime.now(tz=dt.UTC) - DAILY_WINDOW
    total = (
        await db.execute(
            select(func.coalesce(func.sum(m.EmailOtpChallenge.attempts), 0)).where(
                m.EmailOtpChallenge.member_id == member_id,
                m.EmailOtpChallenge.created_at > since,
            )
        )
    ).scalar_one()
    return int(total)


def _generate_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


async def issue_code(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, purpose: Purpose
) -> tuple[uuid.UUID, str]:
    """Store a fresh code and return (challenge id, plaintext code).

    Raises `Capped` once the member reached the daily cap on wrong codes, and
    `CoolingDown` when a code of this purpose went out less than a minute ago.
    Every older unused code of the same purpose is spent, so only the newest
    one can be answered.
    """
    if await failed_attempts_today(db, member_id) >= DAILY_FAILED_ATTEMPTS:
        raise Capped
    now = dt.datetime.now(tz=dt.UTC)
    recent = (
        await db.execute(
            select(m.EmailOtpChallenge.id)
            .where(
                m.EmailOtpChallenge.member_id == member_id,
                m.EmailOtpChallenge.purpose == purpose,
                m.EmailOtpChallenge.created_at > now - dt.timedelta(seconds=RESEND_SECONDS),
                # A code whose mail failed is spent at once and does not hold
                # the retry back; one that was answered does not either.
                m.EmailOtpChallenge.used_at.is_(None),
            )
            .limit(1)
        )
    ).first()
    if recent is not None:
        raise CoolingDown
    await db.execute(
        update(m.EmailOtpChallenge)
        .where(
            m.EmailOtpChallenge.member_id == member_id,
            m.EmailOtpChallenge.purpose == purpose,
            m.EmailOtpChallenge.used_at.is_(None),
        )
        .values(used_at=now)
    )
    code = _generate_code()
    row = m.EmailOtpChallenge(
        tenant_id=tenant_id,
        member_id=member_id,
        purpose=purpose,
        code_hash=hash_password(code),
        expires_at=now + CODE_TTL,
    )
    db.add(row)
    await db.flush()
    return row.id, code


async def spend(db: AsyncSession, challenge_id: uuid.UUID) -> None:
    """Make a code unusable, e.g. when its mail could not be sent."""
    await db.execute(
        update(m.EmailOtpChallenge)
        .where(m.EmailOtpChallenge.id == challenge_id)
        .values(used_at=dt.datetime.now(tz=dt.UTC))
    )


async def check_code(
    db: AsyncSession, *, member_id: uuid.UUID, purpose: Purpose, code: str
) -> CheckResult:
    """Answer the member's newest live code of this purpose.

    The row is locked (`FOR UPDATE`) so two concurrent answers cannot both
    accept it or both count one attempt. The caller must commit even when
    this refuses, or the attempt it counted is rolled back.
    """
    failed = await failed_attempts_today(db, member_id)
    if failed >= DAILY_FAILED_ATTEMPTS:
        return CheckResult.CAPPED
    now = dt.datetime.now(tz=dt.UTC)
    row = (
        await db.execute(
            select(m.EmailOtpChallenge)
            .where(
                m.EmailOtpChallenge.member_id == member_id,
                m.EmailOtpChallenge.purpose == purpose,
                m.EmailOtpChallenge.used_at.is_(None),
                m.EmailOtpChallenge.expires_at > now,
                m.EmailOtpChallenge.attempts < MAX_ATTEMPTS,
            )
            .order_by(m.EmailOtpChallenge.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return CheckResult.REFUSED
    if len(code) == 6 and code.isdigit() and verify_password(code, row.code_hash):
        row.used_at = now
        return CheckResult.OK
    row.attempts += 1
    if row.attempts >= MAX_ATTEMPTS or failed + 1 >= DAILY_FAILED_ATTEMPTS:
        return CheckResult.LOCKED
    return CheckResult.REFUSED


def code_mail(code: str, purpose: Purpose) -> tuple[str, str]:
    """(subject, body) in English and German: no per-member language is
    stored, so both go in one mail."""
    if purpose == "enrol":
        subject = "Your oc8 confirmation code / Ihr oc8-Bestätigungscode"
        en = "Enter this code to turn on sign-in codes by email:"
        de = "Geben Sie diesen Code ein, um Anmeldecodes per E-Mail einzuschalten:"
    else:
        subject = "Your oc8 sign-in code / Ihr oc8-Anmeldecode"
        en = "Enter this code to finish signing in:"
        de = "Geben Sie diesen Code ein, um die Anmeldung abzuschließen:"
    body = (
        f"{en}\n\n    {code}\n\n"
        "The code is valid for 10 minutes. If you did not ask for it, "
        "someone may know your password: change it.\n\n"
        "---\n\n"
        f"{de}\n\n    {code}\n\n"
        "Der Code ist 10 Minuten gültig. Wenn Sie ihn nicht angefordert haben, "
        "kennt möglicherweise jemand Ihr Passwort: Ändern Sie es."
    )
    return subject, body
