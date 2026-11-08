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
from dataclasses import dataclass

import redis.asyncio as redis

from oc8.config import get_settings
from oc8.oauth.errors import OAuthStateInvalid

STATE_TTL_SECONDS = 600
_KEY_PREFIX = "oc8:oauth:state:"


@dataclass(frozen=True)
class OAuthState:
    tenant_id: uuid.UUID
    user_id: str | None
    member_id: uuid.UUID
    owner: str
    role: str
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
    role: str = "member",
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
            "role": role,
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
    if "owner" not in data or "member_id" not in data:
        raise OAuthStateInvalid("state from an older release; start again")
    return OAuthState(
        tenant_id=uuid.UUID(data["tenant_id"]),
        user_id=data["user_id"],
        member_id=uuid.UUID(data["member_id"]),
        owner=data["owner"],
        role=data.get("role", "member"),
        provider=data["provider"],
        verifier=data["verifier"],
        scopes=tuple(data["scopes"]),
    )
