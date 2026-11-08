"""Microsoft's delegated flow lives on /organizations, its app-only flow keeps
the tenant-scoped URL; both providers fall back to platform clients; Google
asks narrow scopes unless full scopes are switched on."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

from oc8 import config
from oc8 import models as m
from oc8.oauth import http as oauth_http
from oc8.oauth.client import resolve_client
from oc8.oauth.errors import OAuthConsentRequired, OAuthReauthRequired
from oc8.oauth.providers import (
    GOOGLE_FULL_SCOPES,
    GOOGLE_NARROW_SCOPES,
    MICROSOFT_TOOL_SCOPES,
    get_provider,
    tool_scopes,
)
from oc8.oauth.tokens import (
    exchange_code,
    fetch_account_label,
    get_access_token,
    refresh_ref,
    tenant_from_id_token,
)
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    s = config.get_settings()
    monkeypatch.setattr(s, "secret_kek", base64.b64encode(bytes(range(32))).decode(), raising=False)
    monkeypatch.setattr(s, "microsoft_oauth_client_id", "plat-ms-id", raising=False)
    monkeypatch.setattr(s, "microsoft_oauth_client_secret", "plat-ms-sec", raising=False)
    monkeypatch.setattr(s, "google_full_scopes", False, raising=False)


class _Recorder:
    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for needle, response in self.responses.items():
            if needle in str(request.url):
                return response
        return httpx.Response(404, json={})


@pytest.fixture
def recorder() -> Iterator[_Recorder]:
    rec = _Recorder({})
    oauth_http.set_transport_override(httpx.MockTransport(rec.handler))
    yield rec
    oauth_http.set_transport_override(None)


def test_microsoft_delegated_endpoints_and_scopes() -> None:
    p = get_provider("microsoft")
    assert (
        p.authorize_url == "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
    )
    assert p.token_url == "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"
    assert p.app_token_url == "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    assert p.app_scope == "https://graph.microsoft.com/.default"
    assert p.userinfo_url == "https://graph.microsoft.com/v1.0/me"
    assert tool_scopes("microsoft") == MICROSOFT_TOOL_SCOPES
    for scope in (
        "openid",
        "offline_access",
        "User.Read",
        "Mail.ReadWrite",
        "Mail.Send",
        "Calendars.ReadWrite",
        "Files.ReadWrite.All",
        "Sites.Read.All",
    ):
        assert scope in MICROSOFT_TOOL_SCOPES


def test_google_tool_scopes_are_narrow_until_switched(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tool_scopes("google") == GOOGLE_NARROW_SCOPES
    assert "https://www.googleapis.com/auth/drive.file" in GOOGLE_NARROW_SCOPES
    assert not any("readonly" in s or s.endswith("/drive") for s in GOOGLE_NARROW_SCOPES)
    monkeypatch.setattr(config.get_settings(), "google_full_scopes", True, raising=False)
    assert tool_scopes("google") == GOOGLE_FULL_SCOPES
    assert get_provider("google").extra_authorize_params["include_granted_scopes"] == "true"


async def test_microsoft_falls_back_to_the_platform_client(app_session: AppSessionFactory) -> None:
    async with app_session(uuid.uuid4()) as db:
        client = await resolve_client(db, tenant_id=uuid.uuid4(), provider_id="microsoft")
    assert (client.client_id, client.source) == ("plat-ms-id", "platform")


async def test_a_delegated_microsoft_refresh_uses_the_organizations_endpoint(
    app_session: AppSessionFactory, recorder: _Recorder
) -> None:
    recorder.responses["/token"] = httpx.Response(
        200, json={"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = m.OAuthConnection(
            tenant_id=tenant,
            provider="microsoft",
            account_label="anna@contoso.com",
            scopes=[],
            access_secret_ref="",
            client_source="platform",
            grant_type="authorization_code",
            expires_at=datetime.now(tz=UTC) - timedelta(seconds=5),
        )
        db.add(conn)
        await db.flush()
        await store_secret(
            db, tenant_id=tenant, name=refresh_ref(conn.id), value="rt1", kind="oauth_token"
        )
        conn.refresh_secret_ref = refresh_ref(conn.id)
        await db.flush()
        assert await get_access_token(db, tenant_id=tenant, connection_id=conn.id) == "at2"
    assert (
        str(recorder.requests[0].url)
        == "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"
    )


async def test_the_graph_profile_names_the_account(recorder: _Recorder) -> None:
    recorder.responses["graph.microsoft.com/v1.0/me"] = httpx.Response(
        200, json={"mail": None, "userPrincipalName": "anna@contoso.com"}
    )
    assert (
        await fetch_account_label(provider_id="microsoft", access_token="t") == "anna@contoso.com"
    )


async def test_missing_admin_consent_is_its_own_error(
    app_session: AppSessionFactory, recorder: _Recorder
) -> None:
    recorder.responses["/token"] = httpx.Response(
        400,
        json={
            "error": "invalid_grant",
            "error_description": "AADSTS65001: The user or administrator has not consented",
        },
    )
    async with app_session(uuid.uuid4()) as db:
        client = await resolve_client(db, tenant_id=uuid.uuid4(), provider_id="microsoft")
    with pytest.raises(OAuthConsentRequired):
        await exchange_code(
            provider_id="microsoft", client=client, code="c", verifier="v", redirect_uri="https://x"
        )
    assert issubclass(OAuthConsentRequired, OAuthReauthRequired)


def test_the_tenant_comes_from_the_id_token() -> None:
    token = jwt.encode({"tid": "contoso-tid", "oid": "x"}, "k", algorithm="HS256")
    assert tenant_from_id_token(token) == "contoso-tid"
    assert tenant_from_id_token("not-a-jwt") is None
    assert tenant_from_id_token(None) is None
