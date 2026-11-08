from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.oauth import http as oauth_http

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    s = config.get_settings()
    monkeypatch.setattr(
        s, "secret_kek", base64.b64encode(bytes(range(32))).decode(), raising=False
    )
    monkeypatch.setattr(s, "google_oauth_client_id", "plat-id", raising=False)
    monkeypatch.setattr(s, "google_oauth_client_secret", "plat-sec", raising=False)
    monkeypatch.setattr(s, "oauth_redirect_base_url", "https://api.example.com", raising=False)
    monkeypatch.setattr(s, "frontend_base_url", "https://app.example.com", raising=False)


@pytest.fixture(autouse=True)
def _transport() -> Iterator[None]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600}
            )
        if "userinfo" in str(request.url):
            return httpx.Response(200, json={"sub": "g-1", "email": "person@example.com", "email_verified": True})
        if "revoke" in str(request.url):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    oauth_http.set_transport_override(httpx.MockTransport(handler))
    yield
    oauth_http.set_transport_override(None)


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def test_start_returns_an_authorization_url_with_pkce(redis_url: str) -> None:
    # redis_url must be requested (not just implicitly available) -- it is what
    # points get_settings().redis_url at the testcontainer before oauth/state.py's
    # _client() reads it; see tests/oauth/test_state.py for the same footgun.
    # Without it, put_state/consume_state would silently fall back to the shared
    # docker-compose Redis on 6381, which other sessions in this worktree may
    # also be exercising.
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant))
            assert r.status_code == 200, r.text
            url = httpx.URL(r.json()["authorizationUrl"])
            assert str(url).startswith("https://accounts.google.com/o/oauth2/v2/auth")
            q = dict(url.params)
            assert q["response_type"] == "code"
            assert q["client_id"] == "plat-id"
            assert q["code_challenge_method"] == "S256"
            assert q["code_challenge"]
            assert q["state"]
            assert q["access_type"] == "offline"
            assert q["prompt"] == "consent"
            assert q["redirect_uri"] == "https://api.example.com/api/v1/oauth/google/callback"


async def test_start_requires_org_admin() -> None:
    # No redis_url here: role check 403s before the handler ever calls put_state.
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant, "member")
            )
            assert r.status_code == 403, r.text


async def test_full_connect_flow_creates_a_connection(redis_url: str) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            start = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant)
            )
            state = dict(httpx.URL(start.json()["authorizationUrl"]).params)["state"]

            cb = await c.get(
                f"/api/v1/oauth/google/callback?code=the-code&state={state}",
                follow_redirects=False,
            )
            assert cb.status_code in (302, 307), cb.text
            assert cb.headers["location"].startswith("https://app.example.com")

            listed = await c.get("/api/v1/oauth/connections", headers=_headers(tenant))
            assert listed.status_code == 200, listed.text
            rows = listed.json()
            assert len(rows) == 1
            row = rows[0]
            assert row["provider"] == "google"
            assert row["accountLabel"] == "person@example.com"
            assert row["status"] == "active"
            assert row["clientSource"] == "platform"
            # Token material must never be serialized.
            assert "accessToken" not in row
            assert "refreshToken" not in row
            assert "at1" not in listed.text
            assert "rt1" not in listed.text


async def test_token_exchange_failure_redirects_without_creating_a_connection(
    redis_url: str,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            start = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant)
            )
            state = dict(httpx.URL(start.json()["authorizationUrl"]).params)["state"]

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/token"):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(404, json={})

            oauth_http.set_transport_override(httpx.MockTransport(handler))
            try:
                cb = await c.get(
                    f"/api/v1/oauth/google/callback?code=the-code&state={state}",
                    follow_redirects=False,
                )
            finally:
                oauth_http.set_transport_override(None)

            assert cb.status_code in (302, 307), cb.text
            assert cb.headers["location"].startswith("https://app.example.com")
            assert "error=" in cb.headers["location"]

            listed = await c.get("/api/v1/oauth/connections", headers=_headers(tenant))
            assert listed.json() == []


async def test_userinfo_failure_redirects_without_creating_a_connection(
    redis_url: str,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            start = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant)
            )
            state = dict(httpx.URL(start.json()["authorizationUrl"]).params)["state"]

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/token"):
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "at1",
                            "refresh_token": "rt1",
                            "expires_in": 3600,
                        },
                    )
                if "userinfo" in str(request.url):
                    return httpx.Response(500, json={})
                return httpx.Response(404, json={})

            oauth_http.set_transport_override(httpx.MockTransport(handler))
            try:
                cb = await c.get(
                    f"/api/v1/oauth/google/callback?code=the-code&state={state}",
                    follow_redirects=False,
                )
            finally:
                oauth_http.set_transport_override(None)

            assert cb.status_code in (302, 307), cb.text
            assert cb.headers["location"].startswith("https://app.example.com")
            assert "error=account_unresolved" in cb.headers["location"]

            listed = await c.get("/api/v1/oauth/connections", headers=_headers(tenant))
            assert listed.json() == []


async def test_callback_rejects_an_unknown_state(redis_url: str) -> None:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                "/api/v1/oauth/google/callback?code=x&state=bogus", follow_redirects=False
            )
            assert r.status_code == 400, r.text


async def test_callback_state_cannot_be_replayed(redis_url: str) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            start = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant)
            )
            state = dict(httpx.URL(start.json()["authorizationUrl"]).params)["state"]
            first = await c.get(
                f"/api/v1/oauth/google/callback?code=c1&state={state}", follow_redirects=False
            )
            assert first.status_code in (302, 307)
            second = await c.get(
                f"/api/v1/oauth/google/callback?code=c1&state={state}", follow_redirects=False
            )
            assert second.status_code == 400, second.text


async def test_user_denied_consent_redirects_with_an_error(redis_url: str) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            start = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant)
            )
            state = dict(httpx.URL(start.json()["authorizationUrl"]).params)["state"]
            r = await c.get(
                f"/api/v1/oauth/google/callback?error=access_denied&state={state}",
                follow_redirects=False,
            )
            assert r.status_code in (302, 307)
            assert "error=access_denied" in r.headers["location"]


async def test_connections_are_tenant_scoped(redis_url: str) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            start = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant_a)
            )
            state = dict(httpx.URL(start.json()["authorizationUrl"]).params)["state"]
            await c.get(
                f"/api/v1/oauth/google/callback?code=c&state={state}", follow_redirects=False
            )
            other = await c.get("/api/v1/oauth/connections", headers=_headers(tenant_b))
            assert other.json() == []


async def test_disconnect_removes_the_connection(redis_url: str) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            start = await c.post(
                "/api/v1/oauth/google/start", json={"owner": "organisation"}, headers=_headers(tenant)
            )
            state = dict(httpx.URL(start.json()["authorizationUrl"]).params)["state"]
            await c.get(
                f"/api/v1/oauth/google/callback?code=c&state={state}", follow_redirects=False
            )
            listed = await c.get("/api/v1/oauth/connections", headers=_headers(tenant))
            conn_id = listed.json()[0]["id"]

            gone = await c.delete(
                f"/api/v1/oauth/connections/{conn_id}", headers=_headers(tenant)
            )
            assert gone.status_code == 204, gone.text
            after = await c.get("/api/v1/oauth/connections", headers=_headers(tenant))
            assert after.json() == []


async def test_list_and_delete_require_org_admin() -> None:
    # No redis_url here: role check 403s before any handler body runs.
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/oauth/connections", headers=_headers(tenant, "member"))
            assert r.status_code == 403
            d = await c.delete(
                f"/api/v1/oauth/connections/{uuid.uuid4()}",
                headers=_headers(tenant, "member"),
            )
            assert d.status_code == 403
