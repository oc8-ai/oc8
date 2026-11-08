"""Connecting through the provider: who owns the result is decided at /start
and travels in the single-use state, never in the callback's query."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from sqlalchemy import select

from oc8 import config
from oc8 import models as m
from oc8.oauth import http as oauth_http
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers, member, oauth_row

pytestmark = pytest.mark.asyncio

ID_TOKEN = jwt.encode({"tid": "contoso-tid"}, "k", algorithm="HS256")
CONNECTIONS = "https://app.example.com/connections"


@pytest.fixture(autouse=True)
def _clients(monkeypatch: pytest.MonkeyPatch) -> None:
    s = config.get_settings()
    monkeypatch.setattr(s, "microsoft_oauth_client_id", "plat-ms-id", raising=False)
    monkeypatch.setattr(s, "microsoft_oauth_client_secret", "plat-ms-sec", raising=False)
    monkeypatch.setattr(s, "google_oauth_client_id", "plat-g-id", raising=False)
    monkeypatch.setattr(s, "google_oauth_client_secret", "plat-g-sec", raising=False)


class _Provider:
    """Fake Microsoft: tokens, a profile handed out in call order, and a
    programmable token-endpoint failure."""

    def __init__(self) -> None:
        # (object id, label) per profile lookup, in call order.
        self.accounts = iter([("oid-1", "anna@contoso.com"), ("oid-2", "ben@contoso.com")])
        self.token_error: dict[str, str] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/token"):
            if self.token_error is not None:
                return httpx.Response(400, json=self.token_error)
            return httpx.Response(
                200,
                json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600, "id_token": ID_TOKEN},
            )
        if "graph.microsoft.com/v1.0/me" in url:
            oid, label = next(self.accounts)
            return httpx.Response(200, json={"id": oid, "userPrincipalName": label})
        return httpx.Response(404, json={})


@pytest.fixture
def provider() -> Iterator[_Provider]:
    fake = _Provider()
    oauth_http.set_transport_override(httpx.MockTransport(fake.handler))
    yield fake
    oauth_http.set_transport_override(None)


def _params(url: str) -> dict[str, str]:
    return dict(httpx.URL(url).params)


async def _start(c: object, tenant: uuid.UUID, subject: str, provider: str = "microsoft", **body: str) -> dict[str, str]:
    r = await c.post(  # type: ignore[attr-defined]
        f"/api/v1/oauth/{provider}/start", json={"scopeSet": "tools", **body}, headers=headers(tenant, subject)
    )
    assert r.status_code == 200, r.text
    return _params(r.json()["authorizationUrl"]) | {"_url": r.json()["authorizationUrl"]}


async def _callback(c: object, state: str, query: str = "code=c") -> str:
    r = await c.get(  # type: ignore[attr-defined]
        f"/api/v1/oauth/microsoft/callback?{query}&state={state}", follow_redirects=False
    )
    return r.headers["location"]


async def test_a_member_connects_their_own_microsoft_account(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        assert q["_url"].startswith("https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize")
        assert {"offline_access", "Mail.Send", "Calendars.ReadWrite"} <= set(q["scope"].split())
        assert (q["code_challenge_method"], q["prompt"], q["client_id"]) == ("S256", "select_account", "plat-ms-id")
        assert await _callback(c, q["state"]) == f"{CONNECTIONS}?connected=microsoft"
        mine = (await c.get("/api/v1/connections/mine", headers=headers(tenant, "anna@acme.test"))).json()
    assert [(row["accountLabel"], row["owner"]) for row in mine] == [("anna@contoso.com", "me")]
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert (row.owner_member_id, row.created_by_user_id, row.azure_tenant_id) == (anna, anna, "contoso-tid")
        assert row.provider_metadata["subject"] == "contoso-tid:oid-1"


async def test_state_owner_is_the_member_who_started(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    ben = await member(app_session, tenant, "ben@acme.test")
    async with api() as c:
        anna_q = await _start(c, tenant, "anna@acme.test")
        ben_q = await _start(c, tenant, "ben@acme.test")
        # Returned in the opposite order; each result belongs to its starter.
        await _callback(c, ben_q["state"])
        await _callback(c, anna_q["state"])
    async with app_session(tenant) as db:
        rows = {r.account_label: r.owner_member_id for r in (await db.execute(select(m.OAuthConnection))).scalars()}
    assert rows == {"anna@contoso.com": ben, "ben@contoso.com": anna}  # labels came from the fake in call order


async def test_member_cannot_start_an_organisation_connection(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        r = await c.post(
            "/api/v1/oauth/microsoft/start",
            json={"scopeSet": "tools", "owner": "organisation"},
            headers=headers(tenant, "anna@acme.test"),
        )
        legacy = await c.post(
            "/api/v1/oauth/google/start",
            json={"owner": "me", "scopes": ["https://www.googleapis.com/auth/drive"]},
            headers=headers(tenant, "anna@acme.test"),
        )
    assert r.status_code == 403
    assert legacy.status_code == 422


async def test_an_admin_connects_the_organisation_account(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "boss@acme.test")
    async with api() as c:
        r = await c.post(
            "/api/v1/oauth/microsoft/start",
            json={"scopeSet": "tools", "owner": "organisation"},
            headers=headers(tenant, "boss@acme.test", "org_admin"),
        )
        await _callback(c, _params(r.json()["authorizationUrl"])["state"])
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert row.owner_member_id is None


async def test_an_organisation_connection_is_rechecked_at_the_callback(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    """The admin right the state was started with is not trusted: a member
    whose right was withdrawn in between connects nothing for the organisation."""
    tenant = uuid.uuid4()
    boss = await member(app_session, tenant, "boss@acme.test")
    async with api() as c:
        r = await c.post(
            "/api/v1/oauth/microsoft/start",
            json={"scopeSet": "tools", "owner": "organisation"},
            headers=headers(tenant, "boss@acme.test", "org_admin"),
        )
        state = _params(r.json()["authorizationUrl"])["state"]
        # Forge a state whose starter holds no right at all.
        from oc8.oauth.state import put_state

        weak, _ = await put_state(
            tenant_id=tenant, user_id="boss@acme.test", member_id=boss, owner="organisation",
            provider="microsoft", scopes=("openid",), role="member",
        )
        denied = await _callback(c, weak)
        ok = await _callback(c, state)
    assert denied == f"{CONNECTIONS}?error=access_denied&provider=microsoft"
    assert ok == f"{CONNECTIONS}?connected=microsoft"
    async with app_session(tenant) as db:
        assert len((await db.execute(select(m.OAuthConnection))).scalars().all()) == 1


async def test_a_state_is_single_use(app_session: AppSessionFactory, redis_url: str, provider: _Provider) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        first = await c.get(f"/api/v1/oauth/microsoft/callback?code=c&state={q['state']}", follow_redirects=False)
        again = await c.get(f"/api/v1/oauth/microsoft/callback?code=c&state={q['state']}", follow_redirects=False)
    assert first.status_code in (302, 307)
    assert again.status_code == 400


async def test_missing_admin_consent_is_explained(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        denied = await _callback(
            c, q["state"], "error=access_denied&error_description=AADSTS90094%3A+admin+approval+required"
        )
        q2 = await _start(c, tenant, "anna@acme.test")
        provider.token_error = {"error": "invalid_grant", "error_description": "AADSTS65001: not consented"}
        failed = await _callback(c, q2["state"])
    want = f"{CONNECTIONS}?error=admin_consent_required&provider=microsoft"
    assert denied == want
    assert failed == want


async def test_a_single_tenant_registration_is_named(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    provider.token_error = {"error": "invalid_request", "error_description": "AADSTS50194: single-tenant"}
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        assert await _callback(c, q["state"]) == f"{CONNECTIONS}?error=tenant_mismatch&provider=microsoft"


async def test_provider_errors_are_typed_never_echoed(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        outcomes = []
        for query in (
            "error=interaction_required&error_description=secret+details",
            "error=server_error&error_description=%3Cscript%3E",
        ):
            q = await _start(c, tenant, "anna@acme.test")
            outcomes.append(await _callback(c, q["state"], query))
    assert outcomes == [
        f"{CONNECTIONS}?error=interaction_required&provider=microsoft",
        f"{CONNECTIONS}?error=provider_error&provider=microsoft",
    ]


async def test_the_same_account_reconnecting_updates_its_row(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    provider.accounts = iter([("oid-1", "anna@contoso.com"), ("oid-1", "anna.new@contoso.com")])
    async with api() as c:
        first = await _start(c, tenant, "anna@acme.test")
        await _callback(c, first["state"])
        second = await _start(c, tenant, "anna@acme.test")
        assert second["prompt"] == "select_account"
        await _callback(c, second["state"])
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert (row.owner_member_id, row.account_label) == (anna, "anna.new@contoso.com")


async def test_another_account_with_the_same_label_never_overwrites(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    provider.accounts = iter([("oid-1", "anna@contoso.com"), ("oid-2", "anna@contoso.com")])
    async with api() as c:
        await _callback(c, (await _start(c, tenant, "anna@acme.test"))["state"])
        async with app_session(tenant) as db:
            before = (await db.execute(select(m.OAuthConnection))).scalar_one()
            before_id, before_ref = before.id, before.access_secret_ref
        conflict = await _callback(c, (await _start(c, tenant, "anna@acme.test"))["state"])
    assert conflict == f"{CONNECTIONS}?error=account_conflict&provider=microsoft"
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert (row.id, row.access_secret_ref, row.owner_member_id) == (before_id, before_ref, anna)
        assert row.provider_metadata["subject"] == "contoso-tid:oid-1"


async def test_another_account_with_a_different_label_gets_its_own_row(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        await _callback(c, (await _start(c, tenant, "anna@acme.test"))["state"])
        await _callback(c, (await _start(c, tenant, "anna@acme.test"))["state"])
    async with app_session(tenant) as db:
        rows = (await db.execute(select(m.OAuthConnection))).scalars().all()
        assert sorted(r.account_label for r in rows) == ["anna@contoso.com", "ben@contoso.com"]


async def test_one_owners_callback_never_touches_anothers_row(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    ben = await member(app_session, tenant, "ben@acme.test")
    # Both people connect the very same provider account.
    provider.accounts = iter([("oid-1", "shared@contoso.com"), ("oid-1", "shared@contoso.com")])
    async with api() as c:
        await _callback(c, (await _start(c, tenant, "anna@acme.test"))["state"])
        await _callback(c, (await _start(c, tenant, "ben@acme.test"))["state"])
    async with app_session(tenant) as db:
        rows = (await db.execute(select(m.OAuthConnection))).scalars().all()
        assert sorted((r.owner_member_id for r in rows), key=str) == sorted([anna, ben], key=str)


async def test_google_asks_consent_only_on_a_first_connect(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        first = await _start(c, tenant, "anna@acme.test", "google")
        await oauth_row(app_session, tenant, owner=anna, provider="google", label="anna@gmail.test")
        again = await _start(c, tenant, "anna@acme.test", "google")
    assert (first["prompt"], first["include_granted_scopes"], first["access_type"]) == ("consent", "true", "offline")
    assert "https://www.googleapis.com/auth/gmail.send" in first["scope"].split()
    assert "prompt" not in again


async def test_a_member_removed_before_the_return_gets_nothing(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        async with app_session(tenant) as db:
            row = await db.get(m.OrgMember, anna)
            assert row is not None
            row.deleted_at = datetime.now(tz=UTC)
        location = await _callback(c, q["state"])
    assert location == f"{CONNECTIONS}?error=member_gone&provider=microsoft"
    async with app_session(tenant) as db:
        assert (await db.execute(select(m.OAuthConnection))).scalars().all() == []


async def test_the_admin_consent_link(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        r = await c.get("/api/v1/oauth/microsoft/admin-consent-url", headers=headers(tenant, "anna@acme.test"))
    assert r.status_code == 200, r.text
    url = httpx.URL(r.json()["url"])
    assert str(url).startswith("https://login.microsoftonline.com/organizations/v2.0/adminconsent")
    assert dict(url.params) == {
        "client_id": "plat-ms-id",
        "scope": "https://graph.microsoft.com/.default",
        "redirect_uri": CONNECTIONS,
    }
    assert "plat-ms-sec" not in r.text
