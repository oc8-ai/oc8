"""Removing a connection everywhere it lives: the provider (best effort), the
secret store, the in-process token cache, and the row. Shared by the
Connections API, the legacy disconnect route and member removal.

add/flush only, never commit -- the caller owns the transaction.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.oauth.errors import OAuthError
from oc8.oauth.http import get_client
from oc8.oauth.providers import get_provider
from oc8.oauth.tokens import delete_tokens, invalidate_delegated_tokens
from oc8.secrets.service import SecretNotFound, delete_secret, resolve_secret

logger = logging.getLogger(__name__)


async def revoke_oauth_connection(db: AsyncSession, conn: m.OAuthConnection) -> None:
    # Only an authorization-code row holds a user's refresh token; for
    # client_credentials/service_account rows `refresh_secret_ref` holds the
    # app's own secret or key, which must never be posted anywhere.
    try:
        revoke_url = get_provider(conn.provider).revoke_url
    except OAuthError:
        revoke_url = None
    if revoke_url and conn.refresh_secret_ref and conn.grant_type == "authorization_code":
        try:
            token = await resolve_secret(db, tenant_id=conn.tenant_id, ref=conn.refresh_secret_ref)
            async with get_client() as http_client:
                await http_client.post(revoke_url, data={"token": token})
        except Exception as exc:  # best effort: a dead provider must not pin secrets forever
            logger.warning("provider revoke failed for %s: %s", conn.id, exc)
    await delete_tokens(db, tenant_id=conn.tenant_id, connection_id=conn.id)
    # The stored tokens are only half of what this connection can still serve:
    # delegated (domain-wide-delegation) tokens live in a process-local cache
    # keyed by connection, not in the secret store, and would otherwise stay
    # servable for up to an hour after the row was removed.
    invalidate_delegated_tokens(conn.id)
    await db.delete(conn)
    await db.flush()


async def delete_secret_refs(
    db: AsyncSession, *, tenant_id: uuid.UUID, refs: Iterable[str]
) -> None:
    """Delete the secrets behind a credential's refs; one already gone is fine."""
    for ref in refs:
        try:
            await delete_secret(db, tenant_id=tenant_id, ref=ref)
        except SecretNotFound:
            continue


async def delete_credential_with_secrets(db: AsyncSession, cred: m.Credential) -> None:
    await delete_secret_refs(
        db, tenant_id=cred.tenant_id, refs=dict(cred.secret_refs or {}).values()
    )
    await db.delete(cred)
    await db.flush()


async def purge_member_connections(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> int:
    """Everything a removed member connected for themselves. Soft-deleting the
    member never fires the FK cascade, so this is what removes it."""
    conns = (
        (
            await db.execute(
                select(m.OAuthConnection).where(
                    m.OAuthConnection.tenant_id == tenant_id,
                    m.OAuthConnection.owner_member_id == member_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for conn in conns:
        await revoke_oauth_connection(db, conn)
    creds = (
        (
            await db.execute(
                select(m.Credential).where(
                    m.Credential.tenant_id == tenant_id, m.Credential.owner_member_id == member_id
                )
            )
        )
        .scalars()
        .all()
    )
    for cred in creds:
        await delete_credential_with_secrets(db, cred)
    return len(conns) + len(creds)
