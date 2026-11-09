from __future__ import annotations

import pytest

from oc8.oauth.errors import OAuthError
from oc8.oauth.providers import get_provider, provider_ids, redirect_uri_for


def test_google_provider_is_registered() -> None:
    p = get_provider("google")
    assert p.provider_id == "google"
    assert p.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert p.token_url == "https://oauth2.googleapis.com/token"
    assert p.revoke_url == "https://oauth2.googleapis.com/revoke"
    assert p.userinfo_url == "https://www.googleapis.com/oauth2/v3/userinfo"


def test_google_requests_offline_access_and_granted_scopes() -> None:
    # access_type=offline is load-bearing (no refresh token without it); the
    # consent prompt is now decided per start (first connect only), and
    # include_granted_scopes makes connecting extend the sign-in grant.
    p = get_provider("google")
    assert p.extra_authorize_params["access_type"] == "offline"
    assert p.extra_authorize_params["include_granted_scopes"] == "true"
    assert p.prompt_first == "consent"


def test_google_default_scopes_include_drive_readonly_and_email() -> None:
    p = get_provider("google")
    assert "https://www.googleapis.com/auth/drive.readonly" in p.default_scopes
    assert "email" in p.default_scopes


def test_unknown_provider_raises() -> None:
    with pytest.raises(OAuthError, match="unknown oauth provider"):
        get_provider("dropbox")


def test_provider_ids_lists_google() -> None:
    # microsoft joined the registry in Task 2 (client_credentials grant); this
    # test only asserts google's presence, not the full roster.
    assert "google" in provider_ids()


def test_redirect_uri_is_derived_from_settings() -> None:
    from oc8 import config

    settings = config.get_settings()
    original = settings.oauth_redirect_base_url
    try:
        object.__setattr__(settings, "oauth_redirect_base_url", "https://app.example.com/")
        assert redirect_uri_for("google") == (
            "https://app.example.com/api/v1/oauth/google/callback"
        )
    finally:
        object.__setattr__(settings, "oauth_redirect_base_url", original)
