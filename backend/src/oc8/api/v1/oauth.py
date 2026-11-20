"""Third-party OAuth: authorization-code flow and connection management (§11.2).

The callback route is deliberately unauthenticated -- the provider redirects the
user's browser to it, so no bearer token exists. The Redis state entry is
therefore the authorization: unguessable, single-use, short-lived, and carrying
the tenant binding. Because no principal is injected there, the handler opens
its own RLS-bound session from the state's tenant_id. The state binds tenant,
member and owner, and the callback lands on the frontend's Connections page.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission, unguarded
from oc8.api.v1.connections import _oauth_dto, caller_member, may_manage_integrations
from oc8.authz.permissions import CONNECTION_OWN, INTEGRATION, MANAGE, VIEW, perm
from oc8.config import get_settings
from oc8.connections.revoke import revoke_oauth_connection
from oc8.oauth.client import ResolvedClient, resolve_client
from oc8.oauth.errors import (
    OAuthAccountUnresolved,
    OAuthConsentRequired,
    OAuthError,
    OAuthNotConfigured,
    OAuthStateInvalid,
    OAuthTenantMismatch,
)
from oc8.oauth.providers import get_provider, redirect_uri_for, tool_scopes
from oc8.oauth.provisioning import provision_oauth_connection
from oc8.oauth.state import OAuthState, consume_state, put_pending, put_state, take_pending
from oc8.oauth.tokens import (
    AccountIdentity,
    TokenResponse,
    access_ref,
    exchange_code,
    expiry_from,
    fetch_account_identity,
    persist_tokens,
    refresh_ref,
    tenant_from_id_token,
)
from oc8.schemas.dto import AdminConsentUrlDTO, CompleteOAuthDTO, OAuthConnectionDTO, OAuthStartDTO
from oc8.schemas.requests import (
    CompleteOAuthRequest,
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
    """Always the Connections page of this deployment's own frontend -- never a
    caller-supplied URL."""
    base = get_settings().frontend_base_url.rstrip("/")
    return RedirectResponse(f"{base}/connections?{urlencode(params)}", status_code=302)


#: Provider-returned `error` values passed through as typed codes; anything else
#: becomes `provider_error`. The raw error_description is never echoed.
_PASS_THROUGH_ERRORS = frozenset({"consent_required", "access_denied", "interaction_required"})


def _provider_error_code(error: str, description: str | None) -> str:
    if any(code in (description or "") for code in ("AADSTS65001", "AADSTS90094")):
        return "admin_consent_required"
    return error if error in _PASS_THROUGH_ERRORS else "provider_error"


@router.post(
    "/oauth/{provider}/start",
    response_model=OAuthStartDTO,
    dependencies=[Depends(require_permission(CONNECTION_OWN))],
)
async def start_oauth(
    provider: str,
    body: StartOAuthRequest,
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
) -> OAuthStartDTO:
    if body.owner == "organisation" and not await may_manage_integrations(request, db, principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requires permission: integration:manage")
    if body.scopes and body.owner != "organisation":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "an explicit scope list is accepted for an organisation connection only",
        )
    me = await caller_member(db, principal)
    try:
        prov = get_provider(provider)
        client = await resolve_client(db, tenant_id=principal.tenant_id, provider_id=provider)
        scopes = tuple(body.scopes) if body.scopes else tool_scopes(provider)
    except OAuthNotConfigured as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except SecretStoreUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    owner_filter = (
        m.OAuthConnection.owner_member_id.is_(None)
        if body.owner == "organisation"
        else m.OAuthConnection.owner_member_id == me.id
    )
    has_active = bool(
        await db.scalar(
            select(func.count())
            .select_from(m.OAuthConnection)
            .where(
                m.OAuthConnection.provider == provider,
                m.OAuthConnection.grant_type == "authorization_code",
                m.OAuthConnection.status == "active",
                owner_filter,
            )
        )
    )
    state, challenge = await put_state(
        tenant_id=principal.tenant_id,
        user_id=principal.subject,
        member_id=me.id,
        owner=body.owner,
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
    prompt = prov.prompt_again if has_active else prov.prompt_first
    if prompt:
        params["prompt"] = prompt
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
    "/oauth/microsoft/admin-consent-url",
    response_model=AdminConsentUrlDTO,
    dependencies=[Depends(require_permission(CONNECTION_OWN))],
)
async def admin_consent_url(db: DbSession, principal: CurrentPrincipal) -> AdminConsentUrlDTO:
    """The link a person hands their IT administrator when their organisation
    lets nobody but an administrator consent (AADSTS65001/90094). Built from the
    client id only -- no secret is involved."""
    try:
        client = await resolve_client(db, tenant_id=principal.tenant_id, provider_id="microsoft")
    except OAuthNotConfigured as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return AdminConsentUrlDTO(url=_consent_link(client.client_id))


def _consent_link(client_id: str) -> str:
    prov = get_provider("microsoft")
    params = {
        "client_id": client_id,
        "scope": prov.app_scope or "https://graph.microsoft.com/.default",
        "redirect_uri": f"{get_settings().frontend_base_url.rstrip('/')}/connections",
    }
    return f"{prov.admin_consent_url}?{urlencode(params)}"


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
    error_description: str | None = None,
) -> RedirectResponse:
    """Validates and consumes the state, then parks the code. It deliberately
    exchanges nothing: whoever completes the provider's consent here may be a
    different person than whoever started it, so the result only becomes a
    connection once the starter confirms it in their own session."""
    if not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing state")
    try:
        st = await consume_state(state)
    except OAuthStateInvalid as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if st.provider != provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state/provider mismatch")
    if error:
        return _frontend_redirect(
            error=_provider_error_code(error, error_description), provider=provider
        )
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing code")
    pending = await put_pending(st, code)
    return _frontend_redirect(pending=pending, provider=provider)


def _failure(error: str, **extra: str) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status.HTTP_400_BAD_REQUEST)


@router.post(
    "/oauth/{provider}/complete",
    response_model=CompleteOAuthDTO,
    dependencies=[Depends(require_permission(CONNECTION_OWN))],
)
async def complete_oauth(
    provider: str,
    body: CompleteOAuthRequest,
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
) -> CompleteOAuthDTO | JSONResponse:
    """The starter confirms the provider's answer from their own session; only
    then is the code exchanged and the account stored."""
    taken = await take_pending(body.pending)
    me = await caller_member(db, principal)
    # Nothing distinguishes "not yours" from "not there": the entry is consumed
    # either way and nothing is persisted.
    if (
        taken is None
        or taken[0].provider != provider
        or taken[0].tenant_id != principal.tenant_id
        or taken[0].member_id != me.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no pending connection")
    st, code = taken
    owner_member_id: uuid.UUID | None = me.id if st.owner == "me" else None
    if owner_member_id is None and not await may_manage_integrations(request, db, principal):
        # Re-checked now: the right may have been taken away since /start.
        return _failure("not_permitted")

    try:
        client = await resolve_client(db, tenant_id=st.tenant_id, provider_id=provider)
        tokens = await exchange_code(
            provider_id=provider,
            client=client,
            code=code,
            verifier=st.verifier,
            redirect_uri=redirect_uri_for(provider),
        )
        identity = await fetch_account_identity(
            provider_id=provider, access_token=tokens.access_token, id_token=tokens.id_token
        )
    except OAuthConsentRequired:
        extra = {}
        if provider == "microsoft":
            extra["adminConsentUrl"] = _consent_link(client.client_id)
        return _failure("admin_consent_required", **extra)
    except OAuthTenantMismatch:
        return _failure("tenant_mismatch")
    except OAuthAccountUnresolved:
        logger.warning("oauth complete: no stable identity for provider=%s", provider)
        return _failure("account_unresolved")
    except OAuthError as exc:
        logger.warning("oauth complete failed for provider=%s: %s", provider, exc)
        return _failure("exchange_failed")
    except Exception:
        logger.warning(
            "oauth complete failed unexpectedly for provider=%s", provider, exc_info=True
        )
        return _failure("exchange_failed")

    conn: m.OAuthConnection | None = None
    for _attempt in range(2):
        try:
            # A savepoint keeps the request's tenant binding alive across a retry.
            async with db.begin_nested():
                conn = await _store_connection(
                    db,
                    st=st,
                    provider=provider,
                    owner_member_id=owner_member_id,
                    client=client,
                    tokens=tokens,
                    identity=identity,
                )
            break
        except IntegrityError:
            # A concurrent completion won the unique constraint: look again once.
            conn = None
    if conn is None:
        return _failure("account_conflict")
    await db.commit()
    names = {me.id: me.display_name}
    return CompleteOAuthDTO(connected=provider, connection=_oauth_dto(conn, me.id, names, {}))


async def _store_connection(
    db: AsyncSession,
    *,
    st: OAuthState,
    provider: str,
    owner_member_id: uuid.UUID | None,
    client: ResolvedClient,
    tokens: TokenResponse,
    identity: AccountIdentity,
) -> m.OAuthConnection | None:
    """Create or refresh the owner's row for this account; None on a label held
    by a different account (never overwritten)."""
    label = identity.label
    owner_filter = (
        m.OAuthConnection.owner_member_id.is_(None)
        if owner_member_id is None
        else m.OAuthConnection.owner_member_id == owner_member_id
    )
    candidates = (
        (
            await db.execute(
                select(m.OAuthConnection).where(
                    m.OAuthConnection.tenant_id == st.tenant_id,
                    m.OAuthConnection.provider == provider,
                    owner_filter,
                )
            )
        )
        .scalars()
        .all()
    )

    def subject_of(c: m.OAuthConnection) -> str | None:
        return (c.provider_metadata or {}).get("subject")

    # The same account is the same subject; a label alone proves nothing --
    # except for a legacy row that never recorded a subject, which is adopted.
    existing = next(
        (
            c
            for c in candidates
            if c.grant_type == "authorization_code" and subject_of(c) == identity.subject
        ),
        None,
    ) or next(
        (
            c
            for c in candidates
            if c.grant_type == "authorization_code"
            and subject_of(c) is None
            and c.account_label == label
        ),
        None,
    )
    if existing is None and any(c.account_label == label for c in candidates):
        return None
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
            grant_type="authorization_code",
            created_by_user_id=st.member_id,
            owner_member_id=owner_member_id,
            azure_tenant_id=(
                tenant_from_id_token(tokens.id_token) if provider == "microsoft" else None
            ),
            provider_metadata={"subject": identity.subject},
        )
        db.add(conn)
        await db.flush()
    else:
        conn = existing
        conn.status = "active"
        conn.client_source = client.source
        conn.provider_metadata = {**(conn.provider_metadata or {}), "subject": identity.subject}
        if not any(c.account_label == label and c.id != conn.id for c in candidates):
            conn.account_label = label
        if tokens.scope:
            conn.scopes = list(tokens.scope.split())

    conn.access_secret_ref = access_ref(conn.id)
    if tokens.refresh_token:
        conn.refresh_secret_ref = refresh_ref(conn.id)
    conn.expires_at = expiry_from(tokens.expires_in)
    await persist_tokens(db, tenant_id=st.tenant_id, connection_id=conn.id, tokens=tokens)
    await db.flush()
    return conn


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

    await revoke_oauth_connection(db, conn)
    await db.commit()
