"""Short-lived, single-use OAuth state + PKCE verifier, held in Redis.

The callback route carries no bearer token -- the provider redirects the user's
browser there -- so this entry IS the authorization. Its properties carry the
whole security burden: 32 bytes of randomness (unguessable), deleted on read
(replay-proof), a 10-minute TTL (narrow window), and an embedded tenant_id,
member_id and owner so the callback cannot be steered at another tenant or
another person.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as redis

from oc8.config import get_settings
from oc8.oauth.errors import OAuthStateInvalid

STATE_TTL_SECONDS = 600
_KEY_PREFIX = "oc8:oauth:state:"
_PENDING_PREFIX = "oc8:oauth:pending:"
PENDING_TTL_SECONDS = 300
OWNERS = ("me", "organisation")


@dataclass(frozen=True)
class OAuthState:
    tenant_id: uuid.UUID
    user_id: str | None
    member_id: uuid.UUID
    owner: str
    provider: str
    verifier: str
    scopes: tuple[str, ...]


def _client() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def put_state(
    *,
    tenant_id: uuid.UUID,
    user_id: str | None,
    member_id: uuid.UUID,
    owner: str,
    provider: str,
    scopes: tuple[str, ...],
) -> tuple[str, str]:
    """Store a new state entry; return `(state, code_challenge)`."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    payload = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "user_id": user_id,
            "member_id": str(member_id),
            "owner": owner,
            "provider": provider,
            "verifier": verifier,
            "scopes": list(scopes),
        }
    )
    client = _client()
    try:
        await client.set(f"{_KEY_PREFIX}{state}", payload, ex=STATE_TTL_SECONDS)
    finally:
        await client.aclose()
    return state, _challenge(verifier)


async def consume_state(state: str) -> OAuthState:
    """Atomically read and delete the entry, or raise `OAuthStateInvalid`."""
    client = _client()
    try:
        raw = await client.getdel(f"{_KEY_PREFIX}{state}")
    finally:
        await client.aclose()
    if raw is None:
        raise OAuthStateInvalid("unknown, expired, or already-used state")
    data = json.loads(raw)
    return _state_from(data)


def _state_from(data: dict[str, Any]) -> OAuthState:
    if "owner" not in data or "member_id" not in data:
        raise OAuthStateInvalid("state from an older release; start again")
    if data["owner"] not in OWNERS:
        raise OAuthStateInvalid("state with an unknown owner")
    return OAuthState(
        tenant_id=uuid.UUID(data["tenant_id"]),
        user_id=data["user_id"],
        member_id=uuid.UUID(data["member_id"]),
        owner=data["owner"],
        provider=data["provider"],
        verifier=data["verifier"],
        scopes=tuple(data["scopes"]),
    )


async def put_pending(st: OAuthState, code: str) -> str:
    """Park an authorised-but-unexchanged code until the member who started the
    flow confirms it from their own session (`/oauth/{provider}/complete`).
    Single-use and short-lived; the id is the only handle."""
    pending = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            **asdict(st),
            "tenant_id": str(st.tenant_id),
            "member_id": str(st.member_id),
            "scopes": list(st.scopes),
            "code": code,
        }
    )
    client = _client()
    try:
        await client.set(f"{_PENDING_PREFIX}{pending}", payload, ex=PENDING_TTL_SECONDS)
    finally:
        await client.aclose()
    return pending


async def take_pending(pending: str) -> tuple[OAuthState, str] | None:
    """Atomically read and delete a parked code; None when unknown or expired."""
    client = _client()
    try:
        raw = await client.getdel(f"{_PENDING_PREFIX}{pending}")
    finally:
        await client.aclose()
    if raw is None:
        return None
    data = json.loads(raw)
    try:
        return _state_from(data), str(data["code"])
    except (OAuthStateInvalid, KeyError, ValueError):
        return None
