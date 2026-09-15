"""CRUD + token lifecycle for a member's own API keys.

A key's authenticated access is never wider than its owner's own live
permissions (resolved fresh, per call, by the outward MCP route -- see
`api/mcp_external.py`) -- so this module carries no scopes/expiry of its own,
only what identifies the key (name, prefix) and what narrows where it may be
used from (allowed_origins).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.models.api_keys import ApiKey

#: Prefixed so a leaked token is recognisable at a glance (in logs, in a
#: scanned repo) the way `ghp_`/`sk-` are for GitHub/OpenAI.
API_KEY_PREFIX = "oc8_ak_"
_SECRET_BYTES = 32
#: How much of the secret to keep around for display ("oc8_ak_4f2a2c9e...")
#: once the full token can never be shown again.
_PREFIX_DISPLAY_CHARS = 8


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Returns (plaintext token, its sha256 hash, its display prefix)."""
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    token = f"{API_KEY_PREFIX}{secret}"
    return token, hash_token(token), secret[:_PREFIX_DISPLAY_CHARS]


async def create_api_key(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    name: str,
    allowed_origins: list[str] | None = None,
) -> tuple[ApiKey, str]:
    """Returns the row and the plaintext token -- shown to the caller once,
    never persisted or retrievable again."""
    token, token_hash, token_prefix = generate_api_key()
    row = ApiKey(
        tenant_id=tenant_id,
        member_id=member_id,
        name=name,
        token_hash=token_hash,
        token_prefix=token_prefix,
        allowed_origins=list(allowed_origins or []),
    )
    db.add(row)
    await db.flush()
    return row, token


async def list_api_keys(db: AsyncSession, *, member_id: uuid.UUID) -> list[ApiKey]:
    rows = (
        await db.execute(
            select(ApiKey).where(ApiKey.member_id == member_id).order_by(ApiKey.created_at.desc())
        )
    ).scalars()
    return list(rows)


async def get_own_api_key(
    db: AsyncSession, *, member_id: uuid.UUID, key_id: uuid.UUID
) -> ApiKey | None:
    """Scoped to `member_id` on every call -- there is no path to another
    member's key, by construction, not by a check a route could forget."""
    return (
        await db.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.member_id == member_id)
        )
    ).scalar_one_or_none()


async def set_enabled(
    db: AsyncSession, *, member_id: uuid.UUID, key_id: uuid.UUID, enabled: bool
) -> ApiKey | None:
    row = await get_own_api_key(db, member_id=member_id, key_id=key_id)
    if row is None:
        return None
    row.enabled = enabled
    await db.flush()
    return row


async def delete_api_key(db: AsyncSession, *, member_id: uuid.UUID, key_id: uuid.UUID) -> bool:
    row = await get_own_api_key(db, member_id=member_id, key_id=key_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def find_enabled_by_token(db: AsyncSession, *, token: str) -> ApiKey | None:
    """Looked up within an already tenant-bound session -- see
    `api/mcp_external.py`'s auth dependency, which resolves the singleton
    tenant first (a token carries no tenant hint of its own)."""
    return (
        await db.execute(
            select(ApiKey).where(ApiKey.token_hash == hash_token(token), ApiKey.enabled.is_(True))
        )
    ).scalar_one_or_none()


async def touch_last_used(db: AsyncSession, api_key: ApiKey) -> None:
    api_key.last_used_at = dt.datetime.now(tz=dt.UTC)
    await db.flush()

