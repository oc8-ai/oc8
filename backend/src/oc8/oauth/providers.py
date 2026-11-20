"""Static third-party OAuth provider registry (tech-spec §11.2).

Two flows per provider can coexist: the delegated authorization-code flow a
person (or an admin, for the organisation) signs in through, and -- Microsoft
only -- the app-only client_credentials flow an admin's own app registration
uses. They need different token endpoints and scopes, so both are declared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from oc8.config import get_settings
from oc8.oauth.errors import OAuthError


@dataclass(frozen=True)
class OAuthProvider:
    provider_id: str
    authorize_url: str
    token_url: str
    revoke_url: str | None
    userinfo_url: str | None
    default_scopes: tuple[str, ...]
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)
    #: client_credentials token endpoint, tenant-scoped by `{tenant_id}`.
    app_token_url: str | None = None
    #: The single scope an app-only token is minted for.
    app_scope: str | None = None
    #: Where an organisation's administrator consents for everybody.
    admin_consent_url: str | None = None
    #: `prompt` when the owner has no active connection yet, and afterwards.
    prompt_first: str | None = None
    prompt_again: str | None = None


#: What connecting a Microsoft account asks for (delegated, incremental on top
#: of sign-in's openid/email/profile). Organisation-wide app-only access stays
#: the admin's own app registration (client_credentials).
MICROSOFT_TOOL_SCOPES: Final = (
    "openid",
    "email",
    "profile",
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Files.ReadWrite.All",
    "Sites.Read.All",
)

#: No restricted scope: usable without a security assessment.
GOOGLE_NARROW_SCOPES: Final = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
)

#: Restricted scopes; only with `OC8_GOOGLE_FULL_SCOPES=true` after verification.
GOOGLE_FULL_SCOPES: Final = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
)

GOOGLE = OAuthProvider(
    provider_id="google",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    revoke_url="https://oauth2.googleapis.com/revoke",
    userinfo_url="https://www.googleapis.com/oauth2/v3/userinfo",
    # The legacy admin path's default (an explicit `scopes` list still wins).
    default_scopes=(
        "openid",
        "email",
        "https://www.googleapis.com/auth/drive.readonly",
    ),
    # access_type=offline is load-bearing: without it Google issues no
    # refresh token. prompt=consent is sent only on a first connect (see
    # prompt_first): with an active connection Google keeps the old refresh
    # token valid and include_granted_scopes adds the new scopes to it.
    extra_authorize_params={"access_type": "offline", "include_granted_scopes": "true"},
    prompt_first="consent",
    prompt_again=None,
)

MICROSOFT = OAuthProvider(
    provider_id="microsoft",
    # Work and school accounts of any organisation; the person's own tenant is
    # read from the id token (`tid`) after the exchange.
    authorize_url="https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
    revoke_url=None,
    userinfo_url="https://graph.microsoft.com/v1.0/me",
    default_scopes=MICROSOFT_TOOL_SCOPES,
    extra_authorize_params={"response_mode": "query"},
    app_token_url="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
    app_scope="https://graph.microsoft.com/.default",
    admin_consent_url="https://login.microsoftonline.com/organizations/v2.0/adminconsent",
    prompt_first="select_account",
    prompt_again="select_account",
)

_PROVIDERS: dict[str, OAuthProvider] = {p.provider_id: p for p in (GOOGLE, MICROSOFT)}


def get_provider(provider_id: str) -> OAuthProvider:
    try:
        return _PROVIDERS[provider_id]
    except KeyError:
        raise OAuthError(f"unknown oauth provider: {provider_id!r}") from None


def provider_ids() -> list[str]:
    return list(_PROVIDERS)


def tool_scopes(provider_id: str) -> tuple[str, ...]:
    """What connecting an account for agents' tools asks for."""
    if provider_id == "google":
        return GOOGLE_FULL_SCOPES if get_settings().google_full_scopes else GOOGLE_NARROW_SCOPES
    if provider_id == "microsoft":
        return MICROSOFT_TOOL_SCOPES
    raise OAuthError(f"no tool scopes for oauth provider: {provider_id!r}")


def redirect_uri_for(provider_id: str) -> str:
    base = get_settings().oauth_redirect_base_url.rstrip("/")
    return f"{base}/api/v1/oauth/{provider_id}/callback"
