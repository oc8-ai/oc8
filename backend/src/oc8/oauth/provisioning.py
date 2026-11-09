"""Provider-keyed OAuth connection provisioning (tech-spec §11.2).

Two call sites need to turn "an admin typed an app registration's credentials"
into a working OAuthConnection: the `/oauth/{provider}/service-connection`
endpoint, and the generic plugin setup form (`api/v1/capas.py`, for a
manifest carrying `[plugin.setup.oauth_provision]`). Keeping the minting itself
here -- rather than inline in either caller -- is what lets both share one
source of truth, and it mirrors the existing static registry in
`oc8.oauth.providers` instead of teaching core a second way to know about a
provider.

Transaction discipline follows `oc8.oauth.tokens`: add/flush only, never commit
and never roll back. The caller owns the transaction, so a caller that has
already written other rows (the setup form stores plugin secrets first) decides
what a failure here means for them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.oauth.errors import OAuthError
from oc8.oauth.http import get_client
from oc8.oauth.providers import get_provider
from oc8.oauth.tokens import (
    access_ref,
    get_access_token,
    invalidate_delegated_tokens,
    mint_delegated_token,
    refresh_ref,
)
from oc8.secrets.service import store_secret

ProvisionFn = Callable[[AsyncSession, uuid.UUID, dict[str, str]], Awaitable[m.OAuthConnection]]

#: Microsoft app-only auth needs exactly these three from whoever collects them.
_MICROSOFT_FIELDS = ("azure_tenant_id", "client_id", "client_secret")


def _require(values: dict[str, str], keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if not values.get(key, "").strip()]
    if missing:
        raise ValueError(f"missing required credential fields: {', '.join(missing)}")


#: The cheapest tenant-wide Graph read there is, and the one the plugin's own
#: connector already probes with. Any app-only token that may read anything can
#: read this; a token whose permissions were never admin-consented cannot.
_GRAPH_PROBE_URL = "https://graph.microsoft.com/v1.0/organization"


async def _prove_graph_access(token: str) -> None:
    """Ask Graph one question, so that "the secret is valid" and "the permissions
    were actually consented" stop being the same answer.

    Azure mints a token happily for an app registration with no admin-consented
    Graph permissions at all, so minting proves only half of what the setup form
    reports. Without this, that admin got a green "connection successful" and
    then a 403 on every tool call, days later, with nothing pointing back at the
    consent screen they never finished. The knowledge-source path already ran
    this check via the connector's `validate()` -- but only when site IDs were
    given, and site IDs are optional: a tools-only setup skipped it entirely.
    """
    async with get_client() as client:
        resp = await client.get(_GRAPH_PROBE_URL, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 200:
        return
    if resp.status_code in (401, 403):
        # Worded like the connector's own `_api_error`, because it is the same
        # failure and an operator should not have to learn it twice.
        raise OAuthError(
            f"Microsoft Graph rejected the credentials (HTTP {resp.status_code}) — check the "
            "app registration's Graph permissions and admin consent"
        )
    raise OAuthError(f"Microsoft Graph API error (HTTP {resp.status_code}): {resp.text[:200]}")


async def _provision_microsoft(
    db: AsyncSession, tenant_id: uuid.UUID, values: dict[str, str]
) -> m.OAuthConnection:
    """Create -- or update in place -- the client_credentials connection for one
    Azure app registration.

    Resubmitting the same form with a rotated client secret must land on the
    SAME row: OAuthConnection is unique on (tenant_id, provider, account_label)
    and everything that already points at the connection (an McpConnection's
    `oauth_connection_id`, a DataSource) points at its id, so a second row would
    either fail the constraint or orphan those references.
    """
    _require(values, _MICROSOFT_FIELDS)
    azure_tenant_id = values["azure_tenant_id"].strip()
    client_id = values["client_id"].strip()
    client_secret = values["client_secret"].strip()

    existing = (
        await db.execute(
            select(m.OAuthConnection).where(
                m.OAuthConnection.tenant_id == tenant_id,
                m.OAuthConnection.provider == "microsoft",
                m.OAuthConnection.account_label == client_id,
                m.OAuthConnection.owner_member_id.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        conn = existing
        conn.azure_tenant_id = azure_tenant_id
        conn.grant_type = "client_credentials"
        conn.status = "active"
        # A rotated secret makes the cached access token worthless; clearing the
        # expiry forces get_access_token below to re-mint rather than hand back
        # a token minted from the credential that was just replaced.
        conn.expires_at = None
        if conn.refresh_secret_ref is None:
            conn.refresh_secret_ref = refresh_ref(conn.id)
    else:
        conn = m.OAuthConnection(
            tenant_id=tenant_id,
            provider="microsoft",
            # Always the Azure app's actual client_id, never a friendly label:
            # _mint_client_credentials (oauth/tokens.py) sends `account_label`
            # back to Microsoft's token endpoint as `client_id` on every
            # re-mint, so a display name here would silently break every
            # refresh after the first.
            account_label=client_id,
            scopes=[get_provider("microsoft").app_scope or "https://graph.microsoft.com/.default"],
            access_secret_ref=access_ref(
                uuid.uuid4()
            ),  # placeholder id; replaced below once conn.id exists
            client_source="tenant",
            grant_type="client_credentials",
            azure_tenant_id=azure_tenant_id,
            status="active",
        )
        db.add(conn)
        await db.flush()  # now conn.id is real
        conn.access_secret_ref = access_ref(conn.id)
        conn.refresh_secret_ref = refresh_ref(conn.id)  # holds the client secret, see Task 2

    assert conn.refresh_secret_ref is not None
    await store_secret(
        db,
        tenant_id=tenant_id,
        name=conn.refresh_secret_ref,
        value=client_secret,
        kind="oauth_token",
    )
    await db.flush()

    # Prove the credential actually works before the caller commits -- a bad
    # client secret must fail here, not at first sync (same discipline as
    # gdrive_source's own validate() being called at source-creation time).
    # OAuthError propagates: the caller decides how to report and unwind it.
    token = await get_access_token(db, tenant_id=tenant_id, connection_id=conn.id)
    await _prove_graph_access(token)
    return conn


PROVISIONERS: dict[str, ProvisionFn] = {"microsoft": _provision_microsoft}

#: Which submitted setup-form field keys each provisioner reads. Declared next
#: to the provisioner itself so a caller can ask, rather than knowing.
PROVISIONED_FIELDS: dict[str, tuple[str, ...]] = {"microsoft": _MICROSOFT_FIELDS}

#: Google service-account auth needs exactly these three from whoever collects
#: them: the downloaded key file's JSON (whole, not just the private key --
#: `client_email` is read out of it too), and the two capability-configuration
#: strings the unconditional probe below needs to know what to check.
_GOOGLE_FIELDS = ("service_account_key", "shared_drive_ids", "delegated_mailboxes")

#: The delegated (domain-wide-delegation) scope this endpoint's own consent
#: probe requests, AND the exact literal string Task 12's plugin.toml declares
#: as `[plugin.setup.oauth_provision].delegated_scope` -- these two must match
#: BYTE FOR BYTE. Google's delegation authorization is scope-exact: a JWT-bearer
#: assertion requesting any scope string outside what was authorized in the
#: Admin Console is rejected with `unauthorized_client`, so a tenant who
#: authorized "gmail.modify calendar" (per docs/GOOGLE_WORKSPACE.md, Task 12)
#: and then had this probe silently ask for a DIFFERENT scope string (e.g.
#: gmail.readonly) would get "domain-wide delegation is not authorized" on a
#: correctly-configured tenant -- the exact failure this constant exists to
#: prevent. This standalone endpoint has no plugin-manifest context of its own
#: (it is not exclusively called by google_workspace's setup form), so it
#: cannot read `delegated_scope` from a manifest here; keeping one named
#: constant, referenced by comment from Task 12's manifest, is the safeguard.
_DELEGATED_SCOPE = (
    "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar"
)

#: The scope the service account's OWN (non-delegated) identity mints with:
#: Drive read/write, which the Docs, Sheets and Slides APIs also accept, so one
#: scope covers the whole self-identity half of the plugin (see
#: docs/GOOGLE_WORKSPACE.md, which explains this to the admin so nobody
#: "fixes" it into three narrower scopes that then have to be authorized
#: separately).
#:
#: It lives HERE, with the Google-keyed provisioner, and is written onto
#: `OAuthConnection.scopes` at provision time -- `oauth/tokens.py`'s
#: `_mint_service_account` reads the row. That module is shared grant
#: machinery for every provider; a Google URL hardcoded in it (as there was
#: until this branch's final review) would silently hand the SECOND
#: `service_account` provider a Google Drive scope.
_GOOGLE_SELF_SCOPE = "https://www.googleapis.com/auth/drive"


#: The cheapest read that proves, independently for EACH capability the tenant
#: actually submitted, that the credentials work: when a Shared Drive was
#: configured, that "the service account was actually added as a member" (a
#: service account can mint a token for a Shared Drive it was never shared
#: with, and Drive answers that with a 404/403 on this call, not on the mint);
#: when a delegated mailbox was configured, that domain-wide delegation is
#: actually authorized, via a delegated Gmail profile read -- for EVERY listed
#: mailbox, not just the first. Domain-wide delegation is authorized per
#: service account and scope, so one mailbox does prove *delegation*; it does
#: not prove the other addresses exist or are spelled correctly, and a deleted,
#: suspended or misspelled address at index >= 1 is otherwise introduced behind
#: a fully green setup screen and only discovered later as a mailbox whose
#: Gmail/Calendar tools all fail. The probes are cheap
#: (`users/{mailbox}/profile` GETs) and `mint_delegated_token` caches the
#: tokens the bridge will want anyway, so this costs one round trip per
#: mailbox, once, at the moment an operator can still fix the typo.
#: Both probe families run when both capabilities are configured -- one
#: succeeding must never mask the other failing, since a tenant can easily
#: submit valid Drive credentials alongside a delegated-mailbox list that was
#: never authorized in the Admin Console. Mirrors microsoft365's
#: _prove_graph_access: an unconditional, capability-appropriate probe, not one
#: that a tools-only config can skip. When NEITHER capability is configured
#: there is nothing this credential is meant to reach yet, and minting a token
#: (which the caller already did) is the only proof available.
async def _prove_google_access(
    token: str,
    *,
    shared_drive_id: str | None,
    delegated_probes: list[tuple[str, str]],
) -> None:
    async with get_client() as client:
        if shared_drive_id:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/drives/{shared_drive_id}",
                params={"supportsAllDrives": "true"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code in (401, 403, 404):
                raise OAuthError(
                    f"Google Drive rejected the credentials for Shared Drive {shared_drive_id!r} "
                    f"(HTTP {resp.status_code}) -- check the service account was added as a member"
                )
            if resp.status_code != 200:
                raise OAuthError(
                    f"Google Drive API error (HTTP {resp.status_code}): {resp.text[:200]}"
                )
        for mailbox, probe_token in delegated_probes:
            resp = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/{mailbox}/profile",
                headers={"Authorization": f"Bearer {probe_token}"},
            )
            if resp.status_code in (401, 403, 404):
                raise OAuthError(
                    f"Gmail rejected delegated access to {mailbox!r} (HTTP {resp.status_code}) -- "
                    "check domain-wide delegation is authorized for this service account and "
                    "scope, and that this mailbox address exists and is spelled correctly"
                )
            if resp.status_code != 200:
                raise OAuthError(f"Gmail API error (HTTP {resp.status_code}): {resp.text[:200]}")


async def _provision_google(
    db: AsyncSession, tenant_id: uuid.UUID, values: dict[str, str]
) -> m.OAuthConnection:
    _require(values, ("service_account_key",))
    try:
        key_data = json.loads(values["service_account_key"])
    except ValueError as exc:
        raise ValueError(f"service_account_key is not valid JSON: {exc}") from None
    client_email = key_data.get("client_email")
    private_key = key_data.get("private_key")
    if not client_email or not private_key:
        raise ValueError("service_account_key JSON is missing client_email or private_key")

    existing = (
        await db.execute(
            select(m.OAuthConnection).where(
                m.OAuthConnection.tenant_id == tenant_id,
                m.OAuthConnection.provider == "google",
                m.OAuthConnection.account_label == client_email,
                m.OAuthConnection.owner_member_id.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        conn = existing
        conn.grant_type = "service_account"
        conn.status = "active"
        conn.expires_at = None
        # `scopes` is what `_mint_service_account` mints the self-identity token
        # WITH (oauth/tokens.py) -- rewritten on every submission so an older row
        # provisioned before that scope moved out of core self-heals here.
        conn.scopes = [_GOOGLE_SELF_SCOPE]
        if conn.refresh_secret_ref is None:
            conn.refresh_secret_ref = refresh_ref(conn.id)
    else:
        conn = m.OAuthConnection(
            tenant_id=tenant_id,
            provider="google",
            account_label=client_email,
            scopes=[_GOOGLE_SELF_SCOPE],
            access_secret_ref=access_ref(uuid.uuid4()),
            client_source="tenant",
            grant_type="service_account",
            status="active",
        )
        db.add(conn)
        await db.flush()
        conn.access_secret_ref = access_ref(conn.id)
        conn.refresh_secret_ref = refresh_ref(conn.id)

    assert conn.refresh_secret_ref is not None
    await store_secret(
        db, tenant_id=tenant_id, name=conn.refresh_secret_ref, value=private_key, kind="oauth_token"
    )
    await db.flush()
    # The key material just changed (this endpoint IS the documented way to
    # rotate a leaked service-account key -- docs/GOOGLE_WORKSPACE.md). Clearing
    # `expires_at` above forces the SELF token to re-mint; delegated tokens live
    # in their own process-local cache, and without this the probes below would
    # hit that cache and report success using tokens minted from the PREVIOUS
    # private key -- a green setup screen for a rotation nothing verified.
    invalidate_delegated_tokens(conn.id)

    token = await get_access_token(db, tenant_id=tenant_id, connection_id=conn.id)
    shared_drive_ids = [
        p.strip() for p in values.get("shared_drive_ids", "").split(",") if p.strip()
    ]
    delegated_mailboxes = [
        p.strip() for p in values.get("delegated_mailboxes", "").split(",") if p.strip()
    ]
    default_mailbox = values.get("default_mailbox", "").strip()
    if default_mailbox and default_mailbox.casefold() not in {
        addr.casefold() for addr in delegated_mailboxes
    }:
        configured = ", ".join(delegated_mailboxes) or "none configured"
        raise ValueError(
            f"default_mailbox {default_mailbox!r} is not one of the configured "
            f"delegated_mailboxes ({configured}) -- fix this at setup time, not "
            "at first Gmail/Calendar tool call"
        )
    delegated_probes: list[tuple[str, str]] = []
    for mailbox in delegated_mailboxes:
        try:
            probe_token = await mint_delegated_token(
                db,
                tenant_id=tenant_id,
                connection_id=conn.id,
                subject=mailbox,
                scope=_DELEGATED_SCOPE,
            )
        except OAuthError as exc:
            # Google rejects a `sub` claim naming a deleted, suspended or
            # misspelled user at MINT time, with an `unauthorized_client` /
            # `invalid_grant` that names an OAuth grant and not the address the
            # operator typed. Say which mailbox, or a one-character typo in a
            # five-mailbox list is an unguided search.
            raise OAuthError(
                f"Google refused to mint a delegated token for {mailbox!r} ({exc}) -- check "
                "domain-wide delegation is authorized for this service account and scope, "
                "and that this mailbox address exists and is spelled correctly"
            ) from exc
        delegated_probes.append((mailbox, probe_token))
    await _prove_google_access(
        token,
        shared_drive_id=shared_drive_ids[0] if shared_drive_ids else None,
        delegated_probes=delegated_probes,
    )
    return conn


PROVISIONERS["google"] = _provision_google
PROVISIONED_FIELDS["google"] = _GOOGLE_FIELDS


def provisioned_fields(provider: str) -> frozenset[str]:
    """The setup-form fields `provider`'s provisioner consumes itself.

    `api/v1/capas.py`'s generic password-field loop asks this so it does NOT
    also write its own `plugin:{name}:{key}` copy of a credential the
    provisioner stores under the OAuth connection's own ref. Two copies of a
    long-lived client secret, one of which nothing reads, is only blast radius:
    a later revocation flow clearing the connection's copy would leave the
    orphan behind.
    """
    return frozenset(PROVISIONED_FIELDS.get(provider, ()))


async def provision_oauth_connection(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider: str, values: dict[str, str]
) -> m.OAuthConnection:
    """Provision `provider`'s non-interactive connection from `values`.

    Raises ValueError for an unknown provider or missing fields, and OAuthError
    when the provider rejects the credential.
    """
    fn = PROVISIONERS.get(provider)
    if fn is None:
        raise ValueError(f"no OAuth provisioner registered for provider {provider!r}")
    return await fn(db, tenant_id, values)
