"""Provider token exchange, refresh, and fresh-token access (tech-spec §11.2).

Tokens live in the §12.3 secret store as two opaque secrets per connection --
the store holds strings only, so splitting access and refresh avoids inventing
a JSON-value convention. `expires_at` stays a plaintext column so expiry can be
checked without a decrypt.

add/flush only, never commits -- the caller owns the transaction.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import jwt as _pyjwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.oauth.client import ResolvedClient, resolve_client
from oc8.oauth.errors import (
    OAuthConsentRequired,
    OAuthError,
    OAuthExchangeFailed,
    OAuthReauthRequired,
)
from oc8.oauth.http import get_client
from oc8.oauth.providers import get_provider
from oc8.secrets.service import SecretNotFound, delete_secret, resolve_secret, store_secret

EXPIRY_SKEW_SECONDS = 60
_TOKEN_KIND = "oauth_token"


def access_ref(connection_id: uuid.UUID) -> str:
    return f"oauth/{connection_id}/access"


def refresh_ref(connection_id: uuid.UUID) -> str:
    return f"oauth/{connection_id}/refresh"


def _token_url_for(conn: m.OAuthConnection) -> str:
    """The provider's token URL, with Microsoft's {tenant_id} placeholder filled
    in from the connection's own azure_tenant_id. Every other provider's
    token_url has no placeholder, so .format is a no-op for them."""
    provider = get_provider(conn.provider)
    template = provider.app_token_url or provider.token_url
    return template.format(tenant_id=conn.azure_tenant_id or "")


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None
    id_token: str | None = None


def _parse(payload: dict[str, object]) -> TokenResponse:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthExchangeFailed("provider returned no access_token")
    refresh = payload.get("refresh_token")
    expires = payload.get("expires_in")
    scope = payload.get("scope")
    id_token = payload.get("id_token")
    return TokenResponse(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) else None,
        expires_in=int(expires) if isinstance(expires, int | float | str) and expires else None,
        scope=scope if isinstance(scope, str) else None,
        id_token=id_token if isinstance(id_token, str) else None,
    )


def extract_chatgpt_account_id(id_token: str) -> str | None:
    """The chatgpt_account_id claim OpenAI's ChatGPT backend requires as the
    ChatGPT-Account-ID header (Task 7's adapter). Decoded WITHOUT signature
    verification -- this id_token is only ever echoed back to OpenAI as a
    header value, never used for an authorization decision inside oc8, and
    oc8 has no OpenAI signing key to verify against. Never raises: a
    malformed or claim-less token degrades to "no header sent," not a hard
    failure -- the caller decides whether that's fatal."""
    try:
        claims = _pyjwt.decode(id_token, options={"verify_signature": False})
    except _pyjwt.PyJWTError:
        return None
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        return None
    account_id = auth_claims.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) else None


def tenant_from_id_token(id_token: str | None) -> str | None:
    """Microsoft's `tid` claim: the directory the person signed in from.
    Read without signature verification -- it labels the row, it authorises
    nothing (the access token itself is what Graph checks)."""
    if not id_token:
        return None
    try:
        claims = _pyjwt.decode(id_token, options={"verify_signature": False})
    except _pyjwt.PyJWTError:
        return None
    tid = claims.get("tid")
    return tid if isinstance(tid, str) and tid else None


async def _post_token(
    provider_id: str, data: dict[str, str], *, url: str | None = None
) -> dict[str, object]:
    token_url = url if url is not None else get_provider(provider_id).token_url
    async with get_client() as client:
        resp = await client.post(token_url, data=data)
    try:
        body: dict[str, object] = resp.json()
    except ValueError:
        raise OAuthExchangeFailed(f"provider returned non-JSON ({resp.status_code})") from None
    if resp.status_code >= 400:
        err = body.get("error")
        description = body.get("error_description")
        if isinstance(description, str) and (
            "AADSTS65001" in description or "AADSTS90094" in description
        ):
            raise OAuthConsentRequired(
                "an administrator of the account's organisation must consent first"
            )
        if err == "invalid_grant":
            raise OAuthReauthRequired("refresh token rejected by the provider")
        raise OAuthExchangeFailed(f"provider error {resp.status_code}: {err!r}")
    return body


async def exchange_code(
    *,
    provider_id: str,
    client: ResolvedClient,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> TokenResponse:
    return _parse(
        await _post_token(
            provider_id,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
            },
        )
    )


async def fetch_account_label(*, provider_id: str, access_token: str) -> str:
    """Best-effort human label for the connected account."""
    provider = get_provider(provider_id)
    if provider.userinfo_url is None:
        return "unknown"
    async with get_client() as http_client:
        resp = await http_client.get(
            provider.userinfo_url, headers={"Authorization": f"Bearer {access_token}"}
        )
    if resp.status_code >= 400:
        return "unknown"
    try:
        body: dict[str, object] = resp.json()
    except ValueError:
        return "unknown"
    for key in ("email", "mail", "userPrincipalName"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def expiry_from(expires_in: int | None) -> dt.datetime | None:
    if expires_in is None:
        return None
    return dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=expires_in)


async def persist_tokens(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    tokens: TokenResponse,
) -> None:
    """Write access (and, when rotated, refresh) into the secret store."""
    await store_secret(
        db,
        tenant_id=tenant_id,
        name=access_ref(connection_id),
        value=tokens.access_token,
        kind=_TOKEN_KIND,
    )
    if tokens.refresh_token:
        await store_secret(
            db,
            tenant_id=tenant_id,
            name=refresh_ref(connection_id),
            value=tokens.refresh_token,
            kind=_TOKEN_KIND,
        )


async def delete_tokens(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> None:
    for ref in (access_ref(connection_id), refresh_ref(connection_id)):
        try:
            await delete_secret(db, tenant_id=tenant_id, ref=ref)
        except SecretNotFound:
            continue


def _fresh_enough(conn: m.OAuthConnection) -> bool:
    """Is the stored access token good for at least `EXPIRY_SKEW_SECONDS` more?"""
    now = dt.datetime.now(tz=dt.UTC)
    return conn.expires_at is not None and conn.expires_at > now + dt.timedelta(
        seconds=EXPIRY_SKEW_SECONDS
    )


async def _live_connection(
    db: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID, *, lock: bool
) -> m.OAuthConnection:
    """The connection, usable or not at all. `lock=True` adds `FOR UPDATE` and
    re-populates the loaded instance, so the caller sees what the row says NOW
    rather than what it said before it waited for the lock."""
    stmt = select(m.OAuthConnection).where(m.OAuthConnection.id == connection_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if conn is None or conn.tenant_id != tenant_id:
        raise OAuthReauthRequired("connection not found")
    if conn.status != "active":
        raise OAuthReauthRequired(f"connection is {conn.status}")
    return conn


async def get_access_token(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> str:
    """Return a token valid for at least `EXPIRY_SKEW_SECONDS`, refreshing (or,
    for a client_credentials/service_account row, re-minting from the stored
    long-lived credential) if needed.

    The refresh/re-mint path takes a row lock (`FOR UPDATE`) so two concurrent
    syncs on the same connection cannot both refresh: the provider may rotate
    the refresh token, and the loser's write would invalidate the winner's.

    Only that path locks. The lock used to be taken unconditionally, before
    anything had even looked at the expiry -- which was cheap while the only
    caller was a scheduled sync, and stopped being cheap once every gateway
    tool call resolves a token: two agents working the same connection then
    serialised on one row for the length of a far-system round trip, on the
    common case where nothing needed refreshing at all. So: read unlocked,
    answer immediately if the token is still good, and only lock when there is
    a write to make -- re-reading under the lock (`populate_existing`, or the
    identity map would hand back the stale attributes we just read) so a
    refresh that landed while we waited is used instead of repeated.
    """
    conn = await _live_connection(db, tenant_id, connection_id, lock=False)
    if _fresh_enough(conn):
        return await resolve_secret(db, tenant_id=tenant_id, ref=access_ref(connection_id))

    conn = await _live_connection(db, tenant_id, connection_id, lock=True)
    if _fresh_enough(conn):
        return await resolve_secret(db, tenant_id=tenant_id, ref=access_ref(connection_id))

    if conn.grant_type == "service_account":
        return await _mint_service_account(db, conn)
    if conn.grant_type == "client_credentials":
        return await _mint_client_credentials(db, conn)
    if conn.grant_type == "device_code":
        return await _refresh_device_code(db, conn)

    if conn.refresh_secret_ref is None:
        conn.status = "needs_reauth"
        await db.flush()
        raise OAuthReauthRequired("no refresh token stored for this connection")

    try:
        refresh_token = await resolve_secret(db, tenant_id=tenant_id, ref=conn.refresh_secret_ref)
    except SecretNotFound:
        conn.status = "needs_reauth"
        await db.flush()
        raise OAuthReauthRequired("refresh token secret missing") from None

    client = await resolve_client(
        db, tenant_id=tenant_id, provider_id=conn.provider, pinned_source=conn.client_source
    )
    try:
        tokens = _parse(
            await _post_token(
                conn.provider,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                },
            )
        )
    except OAuthReauthRequired:
        conn.status = "needs_reauth"
        await db.flush()
        raise

    await persist_tokens(db, tenant_id=tenant_id, connection_id=connection_id, tokens=tokens)
    conn.expires_at = expiry_from(tokens.expires_in)
    await db.flush()
    return tokens.access_token


async def _mint_client_credentials(db: AsyncSession, conn: m.OAuthConnection) -> str:
    """Re-mint from the stored client secret. No refresh token exists for this
    grant type -- `refresh_secret_ref` holds the long-lived client secret
    itself (see the module docstring and Task 1's migration comment)."""
    assert conn.refresh_secret_ref is not None  # set at connection-creation time (Task 3)
    client_secret = await resolve_secret(db, tenant_id=conn.tenant_id, ref=conn.refresh_secret_ref)
    provider = get_provider(conn.provider)
    tokens = _parse(
        await _post_token(
            conn.provider,
            {
                "grant_type": "client_credentials",
                "client_id": conn.account_label,  # Microsoft's client_id; see Task 3's note
                "client_secret": client_secret,
                "scope": provider.app_scope or provider.default_scopes[0],
            },
            url=_token_url_for(conn),
        )
    )
    await persist_tokens(db, tenant_id=conn.tenant_id, connection_id=conn.id, tokens=tokens)
    conn.expires_at = expiry_from(tokens.expires_in)
    await db.flush()
    return tokens.access_token


async def _refresh_device_code(db: AsyncSession, conn: m.OAuthConnection) -> str:
    """JSON refresh_token grant against OpenAI's token endpoint (quirk 6 of
    openai_chatgpt_params.py's docstring: JSON here, form-encoded for the
    code exchange, both against the same TOKEN_URL). Bypasses
    resolve_client()/get_provider() -- a device-code connection uses a
    public client_id (openai_chatgpt_params.CLIENT_ID) with no client_secret
    and no entry in oc8.oauth.providers (see that module's own docstring).
    Refresh tokens ROTATE on this provider -- persist_tokens below always
    writes whatever refresh_token this response returned, even though most
    other providers' refresh responses omit one (meaning "unchanged")."""
    from oc8.oauth.openai_chatgpt_params import CLIENT_ID, TOKEN_URL

    if conn.refresh_secret_ref is None:
        conn.status = "needs_reauth"
        await db.flush()
        raise OAuthReauthRequired("no refresh token stored for this connection")
    try:
        refresh_token = await resolve_secret(
            db, tenant_id=conn.tenant_id, ref=conn.refresh_secret_ref
        )
    except SecretNotFound:
        conn.status = "needs_reauth"
        await db.flush()
        raise OAuthReauthRequired("refresh token secret missing") from None
    async with get_client() as client:
        resp = await client.post(
            TOKEN_URL,
            json={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    if resp.status_code >= 400:
        body: dict[str, object] = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        error = body.get("error")
        # Both of these mean the stored refresh token is permanently dead, so
        # the row has to say so -- otherwise nothing ever prompts the operator
        # to reconnect and the connection just fails identically forever.
        # `invalid_grant` is what a REVOKED or expired refresh token actually
        # returns and used to fall through to the generic OAuthExchangeFailed
        # below with `status` left "active"; `_post_token` (the non-device-code
        # refresh path in this same module) has always mapped it to
        # OAuthReauthRequired, and this mirrors it.
        if error in ("refresh_token_reused", "invalid_grant"):
            conn.status = "needs_reauth"
            await db.flush()
            raise OAuthReauthRequired(
                "refresh token was already rotated -- reauth required"
                if error == "refresh_token_reused"
                else "refresh token rejected by the provider"
            )
        raise OAuthExchangeFailed(f"refresh failed ({resp.status_code})")
    try:
        refreshed: dict[str, object] = resp.json()
    except ValueError:
        # Every other JSON-body read in this module wraps this; a non-JSON 200
        # was the one that escaped as a bare ValueError.
        raise OAuthExchangeFailed(f"provider returned non-JSON ({resp.status_code})") from None
    tokens = _parse(refreshed)
    await persist_tokens(db, tenant_id=conn.tenant_id, connection_id=conn.id, tokens=tokens)
    conn.expires_at = expiry_from(tokens.expires_in)
    if tokens.id_token:
        account_id = extract_chatgpt_account_id(tokens.id_token)
        if account_id:
            conn.provider_metadata = {**conn.provider_metadata, "chatgpt_account_id": account_id}
    await db.flush()
    return tokens.access_token


_JWT_LIFETIME_SECONDS = 3600

#: In-process cache for delegated (domain-wide-delegation) tokens, keyed on
#: (tenant_id, connection_id, subject) -- NOT persisted, and deliberately
#: separate from `access_secret_ref`/`expires_at`, which cache exactly one
#: identity per connection (the self-identity case `_mint_service_account`
#: already uses).
#: A connection with N delegated mailboxes gets a brand-new, un-pooled bridge
#: PER TOOL CALL (`mcp_pool.py`'s `reusable=False` for any oauth-* ref -- see
#: Task 3), so without this cache every tool call -- including ones that touch
#: none of the delegated mailboxes -- would pay one JWT-sign-plus-HTTP-round-trip
#: per configured mailbox. `EXPIRY_SKEW_SECONDS` (already defined above) is the
#: same freshness margin `get_access_token`'s own cache uses.
#:
#: `tenant_id` is part of the key even though `connection_id` is already a UUID:
#: this dict is process-wide and shared by every tenant, and a cache that can be
#: read without the tenant ever being compared is exactly the kind of gap that
#: only stays theoretical until something else starts passing connection ids
#: around. `mint_delegated_token` ALSO validates the row before consulting the
#: cache, so `status`/ownership are re-checked on a hit -- see there.
_delegated_token_cache: dict[tuple[uuid.UUID, uuid.UUID, str], tuple[str, dt.datetime]] = {}


def invalidate_delegated_tokens(connection_id: uuid.UUID) -> None:
    """Forget every cached delegated token for one connection.

    Two callers, both cases where a cached token outlives the authority it was
    minted under:

    * `oauth/provisioning.py` when a service account's key material is stored
      again (`docs/GOOGLE_WORKSPACE.md` tells an admin to rotate a leaked key by
      resubmitting the setup form). Google access tokens outlive the key that
      minted them, so without this the setup probe would hit the cache and
      report success for a delegation configuration that was never re-verified
      against the NEW key -- a false green on the one flow whose entire purpose
      is to verify the new key.
    * `api/v1/oauth.py`'s disconnect, which deletes the row and its secrets; a
      still-cached delegated token would otherwise stay servable for up to an
      hour after the connection an operator believes they revoked is gone.
    """
    for key in [k for k in _delegated_token_cache if k[1] == connection_id]:
        del _delegated_token_cache[key]


def _build_service_account_jwt(
    *, client_email: str, private_key_pem: str, scope: str, token_url: str, subject: str | None
) -> str:
    now = dt.datetime.now(tz=dt.UTC)
    claims: dict[str, object] = {
        "iss": client_email,
        "scope": scope,
        "aud": token_url,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=_JWT_LIFETIME_SECONDS)).timestamp()),
    }
    if subject:
        claims["sub"] = subject
    return _pyjwt.encode(claims, private_key_pem, algorithm="RS256")


async def _post_service_account_assertion(conn: m.OAuthConnection, assertion: str) -> TokenResponse:
    """Posts to `conn.provider`'s own token URL -- never a literal `"google"`,
    matching `_mint_client_credentials`'s existing pattern (`_token_url_for`,
    `conn.provider`) rather than hardcoding a vendor string in this shared
    module."""
    return _parse(
        await _post_token(
            conn.provider,
            {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            url=_token_url_for(conn),
        )
    )


async def _mint_service_account(db: AsyncSession, conn: m.OAuthConnection) -> str:
    """Re-mint the connection's own (non-delegated) token, scoped to whatever
    the provisioner wrote onto `conn.scopes`. Cached normally via
    `access_secret_ref`/`expires_at`, exactly like `_mint_client_credentials`
    -- this is the single-identity case (Shared Drive access, the setup-time
    consent probe), not domain-wide delegation.

    The scope comes off the ROW, never from a constant here. A hardcoded
    `https://www.googleapis.com/auth/drive` used to live in this module, which
    `mint_delegated_token`'s docstring (just below) correctly calls out as the
    vendor-specific-logic-in-core this plan's Global Constraints forbid: the
    second `service_account` provider to arrive would silently be handed a
    Google Drive scope and discover it as an opaque runtime rejection. The
    per-provider value lives with the per-provider provisioner
    (`oauth/provisioning.py`), next to the delegated scope it already owns."""
    assert conn.refresh_secret_ref is not None  # holds the PEM private key
    scope = " ".join(conn.scopes or [])
    if not scope:
        raise OAuthError(
            f"connection {conn.id} has no scopes recorded, so there is nothing to mint a "
            "service-account token FOR -- re-submit this integration's setup form, which "
            "is what writes the provider's self-identity scope onto the connection"
        )
    private_key_pem = await resolve_secret(
        db, tenant_id=conn.tenant_id, ref=conn.refresh_secret_ref
    )
    assertion = _build_service_account_jwt(
        client_email=conn.account_label,
        private_key_pem=private_key_pem,
        scope=scope,
        token_url=get_provider(conn.provider).token_url,
        subject=None,
    )
    tokens = await _post_service_account_assertion(conn, assertion)
    await persist_tokens(db, tenant_id=conn.tenant_id, connection_id=conn.id, tokens=tokens)
    conn.expires_at = expiry_from(tokens.expires_in)
    await db.flush()
    return tokens.access_token


async def mint_delegated_token(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, subject: str, scope: str
) -> str:
    """Mint a token impersonating `subject` (domain-wide delegation) via this
    service account's stored private key, cached per
    `(tenant_id, connection_id, subject)` -- see `_delegated_token_cache`'s
    module-level docstring for why this must be cached, not re-minted per call.
    `scope` is always supplied by the caller (never a default baked in here):
    the manifest-declared `SetupOAuthProvision.delegated_scope` (Task 12) is
    what a plugin actually authorized in the Workspace Admin Console -- a
    hardcoded scope string in this shared module would be exactly the
    vendor-specific-logic-in-core this plan's Global Constraints forbid.

    The row is loaded and validated BEFORE the cache is consulted, matching
    `get_access_token`'s own discipline two screens up. Answering from the
    cache first (as this used to) skipped both the `tenant_id` ownership check
    and the `status == "active"` check for the entire life of a cached entry,
    so a connection revoked or re-keyed a minute ago kept serving tokens.
    `_live_connection` is one unlocked primary-key SELECT; the round trip the
    cache exists to avoid is the JWT-sign-plus-HTTPS mint below, not this."""
    conn = await _live_connection(db, tenant_id, connection_id, lock=False)
    if conn.grant_type != "service_account":
        raise ValueError(
            f"connection {connection_id} is grant_type={conn.grant_type!r}, not "
            "service_account -- domain-wide delegation only applies to a Google "
            "service account"
        )

    now = dt.datetime.now(tz=dt.UTC)
    cache_key = (tenant_id, connection_id, subject)
    cached = _delegated_token_cache.get(cache_key)
    if cached is not None and cached[1] > now + dt.timedelta(seconds=EXPIRY_SKEW_SECONDS):
        return cached[0]

    assert conn.refresh_secret_ref is not None
    private_key_pem = await resolve_secret(db, tenant_id=tenant_id, ref=conn.refresh_secret_ref)
    assertion = _build_service_account_jwt(
        client_email=conn.account_label,
        private_key_pem=private_key_pem,
        scope=scope,
        token_url=get_provider(conn.provider).token_url,
        subject=subject,
    )
    tokens = await _post_service_account_assertion(conn, assertion)
    expires_at = now + dt.timedelta(seconds=_JWT_LIFETIME_SECONDS)
    _delegated_token_cache[cache_key] = (tokens.access_token, expires_at)
    return tokens.access_token
