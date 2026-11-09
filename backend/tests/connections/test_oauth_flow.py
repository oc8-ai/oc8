"""Connecting through the provider: who owns the result is decided at /start
and travels in the single-use state, never in the callback's query. The
callback only parks the provider's answer; the member who started the flow
confirms it from their own session (/complete)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select

from oc8 import config
from oc8 import models as m
from oc8.authz.permissions import CONNECTION_OWN
from oc8.oauth import http as oauth_http
from oc8.oauth import state as oauth_state
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
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/token"):
            self.token_calls += 1
            if self.token_error is not None:
                return httpx.Response(400, json=self.token_error)
            return httpx.Response(
                200,
                json={
                    "access_token": "at1",
                    "refresh_token": "rt1",
                    "expires_in": 3600,
                    "id_token": ID_TOKEN,
                },
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


async def _start(
    c: Any, tenant: uuid.UUID, subject: str, provider: str = "microsoft", **body: str
) -> dict[str, str]:
    r = await c.post(
        f"/api/v1/oauth/{provider}/start",
        json={"scopeSet": "tools", **body},
        headers=headers(tenant, subject),
    )
    assert r.status_code == 200, r.text
    return _params(r.json()["authorizationUrl"]) | {"_url": r.json()["authorizationUrl"]}


async def _callback(c: Any, state: str, query: str = "code=c") -> str:
    r = await c.get(
        f"/api/v1/oauth/microsoft/callback?{query}&state={state}", follow_redirects=False
    )
    return str(r.headers["location"])


def _pending(location: str) -> str:
    return _params(location)["pending"]


async def _complete(
    c: Any, tenant: uuid.UUID, subject: str, pending: str, role: str = "member"
) -> httpx.Response:
    return await c.post(
        "/api/v1/oauth/microsoft/complete",
        json={"pending": pending},
        headers=headers(tenant, subject, role),
    )


async def _connect(
    c: Any, tenant: uuid.UUID, subject: str, state: str, role: str = "member"
) -> httpx.Response:
    """Provider returns, the starter confirms."""
    return await _complete(c, tenant, subject, _pending(await _callback(c, state)), role)


async def test_a_member_connects_their_own_microsoft_account(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        assert q["_url"].startswith(
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
        )
        assert {"offline_access", "Mail.Send", "Calendars.ReadWrite"} <= set(q["scope"].split())
        assert (q["code_challenge_method"], q["prompt"], q["client_id"]) == (
            "S256",
            "select_account",
            "plat-ms-id",
        )
        location = await _callback(c, q["state"])
        assert location.startswith(f"{CONNECTIONS}?")
        assert provider.token_calls == 0  # nothing is exchanged before the starter confirms
        done = await _complete(c, tenant, "anna@acme.test", _pending(location))
        assert done.status_code == 200, done.text
        assert done.json()["connected"] == "microsoft"
        assert done.json()["connection"]["accountLabel"] == "anna@contoso.com"
        mine = (
            await c.get("/api/v1/connections/mine", headers=headers(tenant, "anna@acme.test"))
        ).json()
    assert [(row["accountLabel"], row["owner"]) for row in mine] == [("anna@contoso.com", "me")]
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert (row.owner_member_id, row.created_by_user_id, row.azure_tenant_id) == (
            anna,
            anna,
            "contoso-tid",
        )
        assert row.provider_metadata["subject"] == "contoso-tid:oid-1"


async def test_someone_else_completing_the_consent_cannot_plant_their_account(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    """A starts a flow and gets B to consent: the code lands in A's pending
    entry; B's own session can neither use nor, once tried, keep it."""
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    await member(app_session, tenant, "ben@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        pending = _pending(await _callback(c, q["state"]))
        as_ben = await _complete(c, tenant, "ben@acme.test", pending)
        as_anna_after = await _complete(c, tenant, "anna@acme.test", pending)
    assert as_ben.status_code == 404
    assert as_anna_after.status_code == 404  # the attempt consumed it
    assert provider.token_calls == 0
    async with app_session(tenant) as db:
        assert (await db.execute(select(m.OAuthConnection))).scalars().all() == []


async def test_a_pending_connection_cannot_cross_tenants(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    await member(app_session, other, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        pending = _pending(await _callback(c, q["state"]))
        crossed = await _complete(c, other, "anna@acme.test", pending)
    assert crossed.status_code == 404
    async with app_session(other) as db:
        assert (await db.execute(select(m.OAuthConnection))).scalars().all() == []


async def test_a_pending_connection_is_single_use_and_expires(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        pending = _pending(await _callback(c, q["state"]))
        first = await _complete(c, tenant, "anna@acme.test", pending)
        again = await _complete(c, tenant, "anna@acme.test", pending)
        # An expired entry is simply gone.
        q2 = await _start(c, tenant, "anna@acme.test")
        gone = _pending(await _callback(c, q2["state"]))
        client = oauth_state._client()
        try:
            await client.delete(f"{oauth_state._PENDING_PREFIX}{gone}")
        finally:
            await client.aclose()
        expired = await _complete(c, tenant, "anna@acme.test", gone)
    assert (first.status_code, again.status_code, expired.status_code) == (200, 404, 404)
    assert oauth_state.PENDING_TTL_SECONDS == 300


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
        await _connect(c, tenant, "ben@acme.test", ben_q["state"])
        await _connect(c, tenant, "anna@acme.test", anna_q["state"])
    async with app_session(tenant) as db:
        rows = {
            r.account_label: r.owner_member_id
            for r in (await db.execute(select(m.OAuthConnection))).scalars()
        }
    # labels came from the fake in call order
    assert rows == {"anna@contoso.com": ben, "ben@contoso.com": anna}


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
        state = _params(r.json()["authorizationUrl"])["state"]
        done = await _connect(c, tenant, "boss@acme.test", state, "org_admin")
        assert done.status_code == 200, done.text
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert row.owner_member_id is None


async def test_a_demoted_admin_connects_nothing_for_the_organisation(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    """Started as an administrator; a role without integration:manage is
    assigned before the starter confirms."""
    tenant = uuid.uuid4()
    boss = await member(app_session, tenant, "boss@acme.test")
    async with api() as c:
        r = await c.post(
            "/api/v1/oauth/microsoft/start",
            json={"scopeSet": "tools", "owner": "organisation"},
            headers=headers(tenant, "boss@acme.test", "org_admin"),
        )
        state = _params(r.json()["authorizationUrl"])["state"]
        pending = _pending(await _callback(c, state))
        async with app_session(tenant) as db:
            role = m.Role(tenant_id=tenant, name="limited", builtin=False, kind="human")
            db.add(role)
            await db.flush()
            db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=CONNECTION_OWN))
            person = await db.get(m.OrgMember, boss)
            assert person is not None
            person.role_id = role.id
        done = await _complete(c, tenant, "boss@acme.test", pending, "org_admin")
    assert (done.status_code, done.json()) == (400, {"error": "not_permitted"})
    assert provider.token_calls == 0
    async with app_session(tenant) as db:
        assert (await db.execute(select(m.OAuthConnection))).scalars().all() == []


async def test_a_state_is_single_use(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        first = await c.get(
            f"/api/v1/oauth/microsoft/callback?code=c&state={q['state']}", follow_redirects=False
        )
        again = await c.get(
            f"/api/v1/oauth/microsoft/callback?code=c&state={q['state']}", follow_redirects=False
        )
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
            c,
            q["state"],
            "error=access_denied&error_description=AADSTS90094%3A+admin+approval+required",
        )
        q2 = await _start(c, tenant, "anna@acme.test")
        provider.token_error = {
            "error": "invalid_grant",
            "error_description": "AADSTS65001: not consented",
        }
        failed = await _connect(c, tenant, "anna@acme.test", q2["state"])
    assert denied == f"{CONNECTIONS}?error=admin_consent_required&provider=microsoft"
    assert failed.status_code == 400
    body = failed.json()
    assert body["error"] == "admin_consent_required"
    link = httpx.URL(body["adminConsentUrl"])
    assert dict(link.params)["client_id"] == "plat-ms-id"
    assert "plat-ms-sec" not in failed.text


async def test_a_single_tenant_registration_is_named(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    provider.token_error = {
        "error": "invalid_request",
        "error_description": "AADSTS50194: single-tenant",
    }
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        done = await _connect(c, tenant, "anna@acme.test", q["state"])
    assert (done.status_code, done.json()) == (400, {"error": "tenant_mismatch"})


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
        await _connect(c, tenant, "anna@acme.test", first["state"])
        second = await _start(c, tenant, "anna@acme.test")
        assert second["prompt"] == "select_account"
        await _connect(c, tenant, "anna@acme.test", second["state"])
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
        await _connect(
            c, tenant, "anna@acme.test", (await _start(c, tenant, "anna@acme.test"))["state"]
        )
        async with app_session(tenant) as db:
            before = (await db.execute(select(m.OAuthConnection))).scalar_one()
            before_id, before_ref = before.id, before.access_secret_ref
        conflict = await _connect(
            c, tenant, "anna@acme.test", (await _start(c, tenant, "anna@acme.test"))["state"]
        )
    assert (conflict.status_code, conflict.json()) == (400, {"error": "account_conflict"})
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert (row.id, row.access_secret_ref, row.owner_member_id) == (
            before_id,
            before_ref,
            anna,
        )
        assert row.provider_metadata["subject"] == "contoso-tid:oid-1"


async def test_a_legacy_row_without_a_subject_is_adopted(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    legacy = await oauth_row(app_session, tenant, owner=anna, label="anna@contoso.com")
    async with api() as c:
        done = await _connect(
            c, tenant, "anna@acme.test", (await _start(c, tenant, "anna@acme.test"))["state"]
        )
    assert done.status_code == 200, done.text
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OAuthConnection))).scalar_one()
        assert row.id == legacy
        assert row.provider_metadata["subject"] == "contoso-tid:oid-1"
        assert row.access_secret_ref != ""


async def test_a_row_with_another_subject_is_not_adopted(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    held = await oauth_row(app_session, tenant, owner=anna, label="anna@contoso.com")
    async with app_session(tenant) as db:
        row = await db.get(m.OAuthConnection, held)
        assert row is not None
        row.provider_metadata = {"subject": "contoso-tid:someone-else"}
    async with api() as c:
        done = await _connect(
            c, tenant, "anna@acme.test", (await _start(c, tenant, "anna@acme.test"))["state"]
        )
    assert (done.status_code, done.json()) == (400, {"error": "account_conflict"})


async def test_another_account_with_a_different_label_gets_its_own_row(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        for _ in range(2):
            q = await _start(c, tenant, "anna@acme.test")
            await _connect(c, tenant, "anna@acme.test", q["state"])
    async with app_session(tenant) as db:
        rows = (await db.execute(select(m.OAuthConnection))).scalars().all()
        assert sorted(r.account_label for r in rows) == ["anna@contoso.com", "ben@contoso.com"]


async def test_one_owners_completion_never_touches_anothers_row(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    ben = await member(app_session, tenant, "ben@acme.test")
    # Both people connect the very same provider account.
    provider.accounts = iter([("oid-1", "shared@contoso.com"), ("oid-1", "shared@contoso.com")])
    async with api() as c:
        for who in ("anna@acme.test", "ben@acme.test"):
            q = await _start(c, tenant, who)
            await _connect(c, tenant, who, q["state"])
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
    assert (first["prompt"], first["include_granted_scopes"], first["access_type"]) == (
        "consent",
        "true",
        "offline",
    )
    assert "https://www.googleapis.com/auth/gmail.send" in first["scope"].split()
    assert "prompt" not in again


async def test_a_member_removed_before_the_return_gets_nothing(
    app_session: AppSessionFactory, redis_url: str, provider: _Provider
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        pending = _pending(await _callback(c, q["state"]))
        async with app_session(tenant) as db:
            row = await db.get(m.OrgMember, anna)
            assert row is not None
            row.deleted_at = datetime.now(tz=UTC)
        done = await _complete(c, tenant, "anna@acme.test", pending)
    assert done.status_code == 404
    async with app_session(tenant) as db:
        assert (await db.execute(select(m.OAuthConnection))).scalars().all() == []


async def test_the_admin_consent_link(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with api() as c:
        r = await c.get(
            "/api/v1/oauth/microsoft/admin-consent-url", headers=headers(tenant, "anna@acme.test")
        )
    assert r.status_code == 200, r.text
    url = httpx.URL(r.json()["url"])
    assert str(url).startswith("https://login.microsoftonline.com/organizations/v2.0/adminconsent")
    assert dict(url.params) == {
        "client_id": "plat-ms-id",
        "scope": "https://graph.microsoft.com/.default",
        "redirect_uri": CONNECTIONS,
    }
    assert "plat-ms-sec" not in r.text


@pytest.mark.parametrize(("failures", "expected"), [(1, 200), (2, 400)])
async def test_a_racing_completion_is_retried_once(
    app_session: AppSessionFactory,
    redis_url: str,
    provider: _Provider,
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
    expected: int,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from oc8.api.v1 import oauth as oauth_api

    real = oauth_api._store_connection
    left = {"n": failures}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        if left["n"] > 0:
            left["n"] -= 1
            raise IntegrityError("insert", {}, Exception("uq_oauth_conn"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(oauth_api, "_store_connection", flaky)
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        q = await _start(c, tenant, "anna@acme.test")
        done = await _connect(c, tenant, "anna@acme.test", q["state"])
    assert done.status_code == expected, done.text
    if expected == 400:
        assert done.json() == {"error": "account_conflict"}
