"""Passkeys and security keys (WebAuthn) as a second factor.

A passkey here never replaces the password: it answers the login challenge
like an authenticator-app code does. Each one is a `second_factor` row whose
`data` holds the credential id and public key (base64url), the sign count and
the transports the browser reported.

The relying party is the configured public URL (`frontend_base_url`): its
host is the relying-party id, its scheme, host and port the only origin a
response is accepted from. A browser creates passkeys only on HTTPS or on
`localhost`, so any other URL, or none, makes the method unavailable; the
policy then treats it as not allowed (`two_factor_policy.load_policy`).

A challenge is stored server-side, bound to the member and the purpose, valid
for five minutes and spent on its first answer, right or wrong. A sign count
that does not increase, where the authenticator counts at all, is a cloned or
replayed credential and is refused.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json_dict
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from oc8 import models as m
from oc8.config import get_settings

Purpose = Literal["register", "login"]

CHALLENGE_TTL = dt.timedelta(minutes=5)
#: What the browser is told to wait for, in milliseconds.
TIMEOUT_MS = 300_000
RP_NAME = "oc8"
METHOD = "passkey"
_TRANSPORTS = {t.value for t in AuthenticatorTransport}


class PasskeyRefused(Exception):
    """A registration or assertion that did not verify. Never says why to
    the caller: the reason is for the log, not for whoever sent it."""


@dataclass(frozen=True)
class RelyingParty:
    id: str
    origin: str


def _is_localhost(host: str) -> bool:
    return host == "localhost" or host.endswith(".localhost")


def relying_party(url: str | None = None) -> RelyingParty | None:
    """The relying party for the public URL (the configured one by default),
    or None when a browser would not create a passkey for it."""
    raw = (get_settings().frontend_base_url if url is None else url).strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # An address is no relying-party id; `localhost` is the way to test.
        return None
    if parts.scheme != "https" and not (parts.scheme == "http" and _is_localhost(host)):
        return None
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
    return RelyingParty(id=host, origin=origin)


def passkey_available() -> bool:
    return relying_party() is not None


# --- challenges ----------------------------------------------------------------


async def issue_challenge(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, purpose: Purpose
) -> bytes:
    """Store a fresh challenge and spend every older live one of the same
    purpose, so only the newest can be answered."""
    now = dt.datetime.now(tz=dt.UTC)
    await db.execute(
        update(m.PasskeyChallenge)
        .where(
            m.PasskeyChallenge.member_id == member_id,
            m.PasskeyChallenge.purpose == purpose,
            m.PasskeyChallenge.used_at.is_(None),
        )
        .values(used_at=now)
    )
    challenge = generate_challenge()
    db.add(
        m.PasskeyChallenge(
            tenant_id=tenant_id,
            member_id=member_id,
            purpose=purpose,
            challenge=bytes_to_base64url(challenge),
            expires_at=now + CHALLENGE_TTL,
        )
    )
    await db.flush()
    return challenge


def generate_challenge() -> bytes:
    return secrets.token_bytes(32)


async def take_challenge(
    db: AsyncSession, *, member_id: uuid.UUID, purpose: Purpose
) -> bytes | None:
    """Spend the member's live challenge of this purpose and return it, or
    None if there is none. Locked (`FOR UPDATE`) so two answers cannot both
    take it; the caller commits before checking the answer, so a wrong one
    spends it too."""
    now = dt.datetime.now(tz=dt.UTC)
    row = (
        await db.execute(
            select(m.PasskeyChallenge)
            .where(
                m.PasskeyChallenge.member_id == member_id,
                m.PasskeyChallenge.purpose == purpose,
                m.PasskeyChallenge.used_at.is_(None),
                m.PasskeyChallenge.expires_at > now,
            )
            .order_by(m.PasskeyChallenge.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.used_at = now
    await db.flush()
    return base64url_to_bytes(row.challenge)


async def spend_challenges(db: AsyncSession, member_id: uuid.UUID) -> None:
    """Spend every live challenge of the member, whatever its purpose."""
    await db.execute(
        update(m.PasskeyChallenge)
        .where(m.PasskeyChallenge.member_id == member_id, m.PasskeyChallenge.used_at.is_(None))
        .values(used_at=dt.datetime.now(tz=dt.UTC))
    )


# --- the member's passkeys --------------------------------------------------------


async def member_passkeys(db: AsyncSession, member_id: uuid.UUID) -> list[m.SecondFactor]:
    """The member's passkeys, oldest first."""
    return list(
        (
            await db.execute(
                select(m.SecondFactor)
                .where(m.SecondFactor.member_id == member_id, m.SecondFactor.method == METHOD)
                .order_by(m.SecondFactor.created_at, m.SecondFactor.id)
            )
        ).scalars()
    )


def _descriptor(data: dict[str, Any]) -> PublicKeyCredentialDescriptor:
    transports = [
        AuthenticatorTransport(t) for t in data.get("transports") or [] if t in _TRANSPORTS
    ]
    return PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(str(data["credential_id"])), transports=transports or None
    )


# --- registration ----------------------------------------------------------------


def registration_options(
    rp: RelyingParty,
    *,
    member: m.OrgMember,
    challenge: bytes,
    existing: list[m.SecondFactor],
) -> dict[str, Any]:
    """`PublicKeyCredentialCreationOptions` as JSON for the browser.

    The user handle is the member row's id: stable, and no personal data.
    A discoverable credential is not asked for -- it is a second factor, the
    password still names the account -- and the passkeys the member already
    holds are excluded, so one authenticator is not registered twice.
    """
    name = member.email or member.subject
    options = generate_registration_options(
        rp_id=rp.id,
        rp_name=RP_NAME,
        user_id=member.id.bytes,
        user_name=name,
        user_display_name=member.display_name or name,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.DISCOURAGED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[_descriptor(row.data) for row in existing],
    )
    return options_to_json_dict(options)


def verify_registration(
    rp: RelyingParty, *, challenge: bytes, credential: dict[str, Any]
) -> dict[str, Any]:
    """Verify a `navigator.credentials.create()` response; the passkey's
    `second_factor.data` on success."""
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
        )
    except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
        raise PasskeyRefused(str(exc)) from exc
    response = credential.get("response")
    reported = response.get("transports") if isinstance(response, dict) else None
    transports = (
        [t for t in reported if isinstance(t, str) and t in _TRANSPORTS]
        if isinstance(reported, list)
        else []
    )
    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": verified.sign_count,
        "transports": transports,
    }


# --- assertion -------------------------------------------------------------------


def authentication_options(
    rp: RelyingParty, *, challenge: bytes, passkeys: list[m.SecondFactor]
) -> dict[str, Any]:
    """`PublicKeyCredentialRequestOptions` as JSON: only the member's own
    passkeys are allowed to answer."""
    options = generate_authentication_options(
        rp_id=rp.id,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        allow_credentials=[_descriptor(row.data) for row in passkeys],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return options_to_json_dict(options)


def credential_id_of(credential: dict[str, Any]) -> str | None:
    raw = credential.get("id")
    return raw if isinstance(raw, str) and raw else None


def verify_assertion(
    rp: RelyingParty, *, challenge: bytes, credential: dict[str, Any], data: dict[str, Any]
) -> int:
    """Verify a `navigator.credentials.get()` response against the stored
    passkey; the new sign count on success.

    py_webauthn refuses a sign count that does not increase when either side
    is non-zero: an authenticator that does not count always sends zero.
    """
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            credential_public_key=base64url_to_bytes(str(data["public_key"])),
            credential_current_sign_count=int(data.get("sign_count") or 0),
        )
    except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
        raise PasskeyRefused(str(exc)) from exc
    return verified.new_sign_count


def sign_count_advances(stored: int, new: int) -> bool:
    """The replay rule, re-checked under the row lock: with either count
    non-zero, the new one must be greater."""
    return (stored == 0 and new == 0) or new > stored
