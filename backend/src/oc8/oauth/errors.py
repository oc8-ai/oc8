"""OAuth client errors (tech-spec §11.2 connector framework, §12.3 secret store)."""

from __future__ import annotations


class OAuthError(Exception):
    """Base for every OAuth-client failure."""


class OAuthNotConfigured(OAuthError):
    """Neither tenant nor platform client credentials are configured."""


class OAuthStateInvalid(OAuthError):
    """The callback's `state` is unknown, expired, or already consumed."""


class OAuthExchangeFailed(OAuthError):
    """The provider rejected a code exchange or refresh."""


class OAuthReauthRequired(OAuthError):
    """The refresh token is dead — the user must re-authorize."""


class OAuthConsentRequired(OAuthReauthRequired):
    """The provider wants an administrator's consent before this account may
    grant the requested access (Microsoft AADSTS65001/AADSTS90094)."""
