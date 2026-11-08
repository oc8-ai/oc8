"""Resolve a provider's client credentials: tenant secrets, else platform env.

Tenant-supplied credentials are the default so that one revoked platform app
verification cannot take every tenant offline at once, and so the provider's
verification burden sits with whoever owns the data (design decision 2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.config import get_settings
from oc8.oauth.errors import OAuthNotConfigured
from oc8.secrets.service import SecretNotFound, resolve_secret


@dataclass(frozen=True)
class ResolvedClient:
    client_id: str
    client_secret: str
    source: str  # "tenant" | "platform"


def tenant_client_id_ref(provider_id: str) -> str:
    return f"oauth/{provider_id}/client_id"


def tenant_client_secret_ref(provider_id: str) -> str:
    return f"oauth/{provider_id}/client_secret"


async def _tenant_client(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider_id: str
) -> ResolvedClient | None:
    try:
        cid = await resolve_secret(db, tenant_id=tenant_id, ref=tenant_client_id_ref(provider_id))
        csec = await resolve_secret(
            db, tenant_id=tenant_id, ref=tenant_client_secret_ref(provider_id)
        )
    except SecretNotFound:
        # A half-configured tenant (id without secret) must fall through
        # entirely rather than yield an unusable client.
        return None
    if not cid or not csec:
        return None
    return ResolvedClient(client_id=cid, client_secret=csec, source="tenant")


def _platform_client(provider_id: str) -> ResolvedClient | None:
    s = get_settings()
    pairs = {
        "google": (s.google_oauth_client_id, s.google_oauth_client_secret),
        "microsoft": (s.microsoft_oauth_client_id, s.microsoft_oauth_client_secret),
    }
    client_id, client_secret = pairs.get(provider_id, ("", ""))
    if not client_id or not client_secret:
        return None
    return ResolvedClient(client_id=client_id, client_secret=client_secret, source="platform")


async def resolve_client(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider_id: str,
    pinned_source: str | None = None,
) -> ResolvedClient:
    """Resolve client credentials, optionally pinned to the source that minted a token.

    `pinned_source` is passed on refresh: re-running the precedence chain would
    break every existing connection the moment a tenant adds its own app.
    """
    if pinned_source == "tenant":
        found = await _tenant_client(db, tenant_id=tenant_id, provider_id=provider_id)
    elif pinned_source == "platform":
        found = _platform_client(provider_id)
    else:
        found = await _tenant_client(
            db, tenant_id=tenant_id, provider_id=provider_id
        ) or _platform_client(provider_id)
    if found is None:
        raise OAuthNotConfigured(
            f"no {provider_id} oauth client configured: store secrets "
            f"{tenant_client_id_ref(provider_id)} and {tenant_client_secret_ref(provider_id)}, "
            f"or set OC8_{provider_id.upper()}_OAUTH_CLIENT_ID / OC8_{provider_id.upper()}_OAUTH_CLIENT_SECRET"
        )
    return found
