"""Third-party OAuth: authorization-code flow and connection management (§11.2).

The callback route is deliberately unauthenticated -- the provider redirects the
user's browser to it, so no bearer token exists. The Redis state entry is
therefore the authorization: unguessable, single-use, short-lived, and carrying
the tenant binding. Because no principal is injected there, the handler opens
its own RLS-bound session from the state's tenant_id.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission, unguarded
from oc8.authz.permissions import INTEGRATION, MANAGE, VIEW, perm
from oc8.config import get_settings
from oc8.db.session import tenant_session
from oc8.oauth.client import resolve_client
from oc8.oauth.errors import (
    OAuthError,
    OAuthNotConfigured,
    OAuthStateInvalid,
)
from oc8.oauth.http import get_client
from oc8.oauth.providers import get_provider, redirect_uri_for
from oc8.oauth.provisioning import provision_oauth_connection
from oc8.oauth.state import consume_state, put_state
from oc8.oauth.tokens import (
    access_ref,
    delete_tokens,
    exchange_code,
    expiry_from,
    fetch_account_label,
    invalidate_delegated_tokens,
    persist_tokens,
    refresh_ref,
)
from oc8.schemas.dto import OAuthConnectionDTO, OAuthStartDTO
from oc8.schemas.requests import (
    CreateGoogleServiceConnectionRequest,
    CreateServiceConnectionRequest,
    StartOAuthRequest,
)
from oc8.secrets.keyprovider import SecretStoreUnavailable

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_dto(conn: m.OAuthConnection) -> OAuthConnectionDTO:
    return OAuthConnectionDTO(
        id=str(conn.id),
        provider=conn.provider,
        account_label=conn.account_label,
        scopes=list(conn.scopes or []),
        status=conn.status,
        client_source=conn.client_source,
        expires_at=str(conn.expires_at) if conn.expires_at else None,
        created_at=str(conn.created_at),
    )


def _frontend_redirect(**params: str) -> RedirectResponse:
    base = get_settings().frontend_base_url.rstrip("/")
    return RedirectResponse(f"{base}/knowledge?{urlencode(params)}", status_code=302)


@router.post(
    "/oauth/{provider}/start",
    response_model=OAuthStartDTO,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def start_oauth(
    provider: str,
    body: StartOAuthRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> OAuthStartDTO:
    try:
        prov = get_provider(provider)
        client = await resolve_client(db, tenant_id=principal.tenant_id, provider_id=provider)
    except OAuthNotConfigured as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except SecretStoreUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    scopes = tuple(body.scopes) if body.scopes else prov.default_scopes
    state, challenge = await put_state(
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
        provider=provider,
        scopes=scopes,
    )
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri_for(provider),
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **dict(prov.extra_authorize_params),
    }
    return OAuthStartDTO(authorization_url=f"{prov.authorize_url}?{urlencode(params)}")


@router.post(
    "/oauth/google/service-connection",
    response_model=OAuthConnectionDTO,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def create_google_service_connection(
    body: CreateGoogleServiceConnectionRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> OAuthConnectionDTO:
    """Non-interactive credential creation for Google's service-account
    JWT-bearer grant — the sibling `create_service_connection` below already
    said this endpoint was coming, rather than trying to widen its own
    `provider` path parameter to a second, unrelated body shape.

    Registered ABOVE `create_service_connection`: this route's path and that
    one's `/oauth/{provider}/service-connection` are the same URL string once
    `{provider}` binds to `"google"`, and FastAPI/Starlette matches routes in
    registration order. If this were registered after the wildcard route,
    every request here would be swallowed by the Microsoft-only handler
    first and fail body validation against `CreateServiceConnectionRequest`
    instead of ever reaching this function.
    """
    try:
        conn = await provision_oauth_connection(
            db,
            tenant_id=principal.tenant_id,
            provider="google",
            values={
                "service_account_key": body.service_account_key_json,
                "shared_drive_ids": body.shared_drive_ids,
                "delegated_mailboxes": body.delegated_mailboxes,
                "default_mailbox": body.default_mailbox,
            },
        )
    except OAuthError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"could not authenticate: {exc}") from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return _to_dto(conn)


@router.post(
    "/oauth/{provider}/service-connection",
    response_model=OAuthConnectionDTO,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def create_service_connection(
    provider: str,
    body: CreateServiceConnectionRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> OAuthConnectionDTO:
    """Non-interactive credential creation for app-only auth (Microsoft
    client_credentials today; the Google Workspace plugin's service_account
    grant will add a sibling endpoint later, not extend this one -- the two
    request shapes don't overlap enough to share a body type)."""
    if provider != "microsoft":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported provider: {provider!r}")

    # The row-building, secret-storing and credential-proving all live in
    # oauth/provisioning.py, because the generic plugin setup form
    # (api/v1/capas.py) provisions the very same connection from its own
    # collected values and must not carry a second copy of this.
    try:
        conn = await provision_oauth_connection(
            db,
            tenant_id=principal.tenant_id,
            provider=provider,
            values={
                "azure_tenant_id": body.azure_tenant_id,
                "client_id": body.client_id,
                "client_secret": body.client_secret,
            },
        )
    except OAuthError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"could not authenticate: {exc}") from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await db.commit()
    return _to_dto(conn)


@router.get(
    "/oauth/{provider}/callback",
    dependencies=[
        Depends(unguarded("browser redirect authenticated by the OAuth state parameter"))
    ],
)
async def oauth_callback(
    provider: str,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing state")
    try:
        st = await consume_state(state)
    except OAuthStateInvalid as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if st.provider != provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state/provider mismatch")
    if error:
        return _frontend_redirect(oauthError=error, provider=provider)
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing code")

    # The state is already single-use-consumed above, so any failure past this
    # point must not become a bare 500: the user would be stuck with no way to
    # retry except starting a fresh /start. Every raise inside the block below
    # propagates through tenant_session's except clause, which rolls back before
    # we ever see it here, so a failure mid-exchange can never commit a
    # half-built connection row.
    try:
        async with tenant_session(st.tenant_id) as db:
            client = await resolve_client(db, tenant_id=st.tenant_id, provider_id=provider)
            tokens = await exchange_code(
                provider_id=provider,
                client=client,
                code=code,
                verifier=st.verifier,
                redirect_uri=redirect_uri_for(provider),
            )
            label = await fetch_account_label(
                provider_id=provider, access_token=tokens.access_token
            )
            if label == "unknown":
                # An unresolvable label would collide with any other account
                # that also failed userinfo, silently overwriting its token
                # refs. Refuse to connect rather than create/overwrite a
                # `(tenant_id, provider, "unknown")` row.
                logger.warning(
                    "oauth callback: could not resolve an account label for "
                    "provider=%s tenant=%s; refusing to create a connection",
                    provider,
                    st.tenant_id,
                )
                return _frontend_redirect(oauthError="account_unresolved", provider=provider)

            existing = (
                await db.execute(
                    select(m.OAuthConnection).where(
                        m.OAuthConnection.tenant_id == st.tenant_id,
                        m.OAuthConnection.provider == provider,
                        m.OAuthConnection.account_label == label,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                conn = m.OAuthConnection(
                    id=uuid.uuid4(),
                    tenant_id=st.tenant_id,
                    provider=provider,
                    account_label=label,
                    scopes=list(tokens.scope.split()) if tokens.scope else list(st.scopes),
                    access_secret_ref="",
                    refresh_secret_ref=None,
                    status="active",
                    client_source=client.source,
                    created_by_user_id=None,
                )
                db.add(conn)
                await db.flush()
            else:
                conn = existing
                conn.status = "active"
                conn.client_source = client.source
                if tokens.scope:
                    conn.scopes = list(tokens.scope.split())

            conn.access_secret_ref = access_ref(conn.id)
            if tokens.refresh_token:
                conn.refresh_secret_ref = refresh_ref(conn.id)
            conn.expires_at = expiry_from(tokens.expires_in)
            await persist_tokens(db, tenant_id=st.tenant_id, connection_id=conn.id, tokens=tokens)
            await db.flush()
    except OAuthError as exc:
        logger.warning(
            "oauth callback exchange failed for provider=%s tenant=%s: %s",
            provider,
            st.tenant_id,
            exc,
        )
        return _frontend_redirect(oauthError="exchange_failed", provider=provider)
    except Exception:
        logger.warning(
            "oauth callback exchange failed unexpectedly for provider=%s tenant=%s",
            provider,
            st.tenant_id,
            exc_info=True,
        )
        return _frontend_redirect(oauthError="exchange_failed", provider=provider)

    return _frontend_redirect(oauthConnected=provider)


@router.get(
    "/oauth/connections",
    response_model=list[OAuthConnectionDTO],
    dependencies=[Depends(require_permission(perm(INTEGRATION, VIEW)))],
)
async def list_connections(
    db: DbSession,
    principal: CurrentPrincipal,
) -> list[OAuthConnectionDTO]:
    rows = (
        (
            await db.execute(
                select(m.OAuthConnection)
                .where(m.OAuthConnection.owner_member_id.is_(None))
                .order_by(m.OAuthConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_to_dto(c) for c in rows]


@router.delete(
    "/oauth/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def disconnect(
    connection_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> None:
    conn = await db.get(m.OAuthConnection, connection_id)
    if conn is None or conn.tenant_id != principal.tenant_id or conn.owner_member_id is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")

    prov = get_provider(conn.provider)
    if prov.revoke_url and conn.refresh_secret_ref:
        # Best effort: a provider-side failure must never block local cleanup,
        # otherwise a dead provider would pin secrets in the store forever.
        try:
            from oc8.secrets.service import resolve_secret

            token = await resolve_secret(db, tenant_id=conn.tenant_id, ref=conn.refresh_secret_ref)
            async with get_client() as http_client:
                await http_client.post(prov.revoke_url, data={"token": token})
        except Exception as exc:  # best effort by design
            logger.warning("provider revoke failed for %s: %s", connection_id, exc)

    await delete_tokens(db, tenant_id=conn.tenant_id, connection_id=conn.id)
    # The stored tokens are only half of what this connection can still serve:
    # delegated (domain-wide-delegation) tokens live in a process-local cache
    # keyed by connection, not in the secret store, and would otherwise stay
    # servable for up to an hour after the row an operator just disconnected.
    invalidate_delegated_tokens(conn.id)
    await db.delete(conn)
    await db.commit()
