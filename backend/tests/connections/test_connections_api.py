from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.oauth.tokens import access_ref, refresh_ref
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, install, member, oauth_row

pytestmark = pytest.mark.asyncio


async def _with_tokens(
    app_session: AppSessionFactory, tenant: uuid.UUID, conn_id: uuid.UUID
) -> None:
    async with app_session(tenant) as db:
        conn = await db.get(m.OAuthConnection, conn_id)
        assert conn is not None
        await store_secret(
            db, tenant_id=tenant, name=access_ref(conn_id), value="at1", kind="oauth_token"
        )
        await store_secret(
            db, tenant_id=tenant, name=refresh_ref(conn_id), value="rt1", kind="oauth_token"
        )
        conn.access_secret_ref = access_ref(conn_id)
        conn.refresh_secret_ref = refresh_ref(conn_id)


async def test_a_member_sees_only_their_own(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    ben = await member(app_session, tenant, "ben@acme.test", name="Ben")
    mine = await oauth_row(app_session, tenant, owner=anna)
    await oauth_row(app_session, tenant, owner=ben, label="ben@acme.test")
    await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    async with api() as c:
        r = await c.get("/api/v1/connections/mine", headers=headers(tenant, "anna@acme.test"))
        assert r.status_code == 200, r.text
        assert [(row["id"], row["owner"]) for row in r.json()] == [(str(mine), "me")]
        for path in ("/api/v1/connections/organisation", "/api/v1/connections/members"):
            assert (await c.get(path, headers=headers(tenant, "anna@acme.test"))).status_code == 403


async def test_admins_see_the_organisation_and_other_members(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    boss = await member(app_session, tenant, "boss@acme.test", name="Boss")
    shared = await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    await oauth_row(
        app_session,
        tenant,
        owner=None,
        provider="openai_chatgpt",
        label="x",
        grant_type="device_code",
    )
    annas = await oauth_row(app_session, tenant, owner=anna)
    await oauth_row(app_session, tenant, owner=boss, label="boss@acme.test")
    admin = headers(tenant, "boss@acme.test", "org_admin")
    async with api() as c:
        org = (await c.get("/api/v1/connections/organisation", headers=admin)).json()
        members = (await c.get("/api/v1/connections/members", headers=admin)).json()
    # The model provider's device-code login is not a connection; the admin's
    # own row is under "mine", never twice.
    assert [row["id"] for row in org] == [str(shared)]
    assert [(row["id"], row["ownerName"], row["owner"]) for row in members] == [
        (str(annas), "Anna", "member")
    ]


async def test_members_listing_carries_no_token_material(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    conn = await oauth_row(app_session, tenant, owner=anna)
    await _with_tokens(app_session, tenant, conn)
    async with api() as c:
        r = await c.get(
            "/api/v1/connections/members", headers=headers(tenant, "admin@acme.test", "org_admin")
        )
    assert r.status_code == 200
    for needle in ("at1", "rt1", "oauth/", "secretRef", "accessSecretRef"):
        assert needle not in r.text


async def test_member_cannot_delete_another_members_connection(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    await member(app_session, tenant, "ben@acme.test")
    annas = await oauth_row(app_session, tenant, owner=anna)
    annas_cred = await credential_row(app_session, tenant, owner=anna)
    org = await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    ben = headers(tenant, "ben@acme.test")
    async with api() as c:
        assert (
            await c.delete(f"/api/v1/connections/oauth/{annas}", headers=ben)
        ).status_code == 404
        assert (
            await c.delete(f"/api/v1/connections/credentials/{annas_cred}", headers=ben)
        ).status_code == 404
        assert (await c.delete(f"/api/v1/connections/oauth/{org}", headers=ben)).status_code == 404
    async with app_session(tenant) as db:
        assert await db.get(m.OAuthConnection, annas) is not None


async def test_a_member_disconnects_their_own(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    conn = await oauth_row(app_session, tenant, owner=anna)
    await _with_tokens(app_session, tenant, conn)
    async with api() as c:
        r = await c.delete(
            f"/api/v1/connections/oauth/{conn}", headers=headers(tenant, "anna@acme.test")
        )
    assert r.status_code == 204, r.text
    async with app_session(tenant) as db:
        assert await db.get(m.OAuthConnection, conn) is None
        assert (
            await db.execute(select(m.Secret).where(m.Secret.tenant_id == tenant))
        ).scalars().all() == []


async def test_admin_can_revoke_a_members_connection(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    conn = await oauth_row(app_session, tenant, owner=anna)
    cred = await credential_row(app_session, tenant, owner=anna)
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        assert (
            await c.delete(f"/api/v1/connections/oauth/{conn}", headers=admin)
        ).status_code == 204
        assert (
            await c.delete(f"/api/v1/connections/credentials/{cred}", headers=admin)
        ).status_code == 204
    async with app_session(tenant) as db:
        assert await db.get(m.OAuthConnection, conn) is None
        assert await db.get(m.Credential, cred) is None
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "connection.revoked")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2


async def test_integrations_lists_connectable_installed_capas(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        for plugin in ("microsoft365", "odoo_mcp"):
            await install(c, admin, plugin)
        r = await c.get(
            "/api/v1/connections/integrations", headers=headers(tenant, "anna@acme.test")
        )
    assert r.status_code == 200, r.text
    by_name = {row["name"]: row for row in r.json()}
    assert by_name["microsoft365"]["kind"] == "oauth"
    assert by_name["microsoft365"]["provider"] == "microsoft"
    assert by_name["microsoft365"]["toolKey"] == "microsoft365"
    assert by_name["odoo_mcp"]["kind"] == "credential"
    assert by_name["odoo_mcp"]["credentialType"] == "odoo_login"
    assert by_name["odoo_mcp"]["toolKey"] == "odoo"
    assert {f["key"] for f in by_name["odoo_mcp"]["credentialFields"]} == {
        "url",
        "database",
        "username",
        "password",
    }
    assert all(row["available"] for row in r.json())


async def test_another_tenants_ids_are_not_found(app_session: AppSessionFactory) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await member(app_session, mine, "anna@acme.test")
    other = await member(app_session, theirs, "zed@other.test")
    conn = await oauth_row(app_session, theirs, owner=other)
    cred = await credential_row(app_session, theirs, owner=other)
    admin = headers(mine, "admin@acme.test", "org_admin")
    async with api() as c:
        assert (
            await c.delete(f"/api/v1/connections/oauth/{conn}", headers=admin)
        ).status_code == 404
        assert (
            await c.delete(f"/api/v1/connections/credentials/{cred}", headers=admin)
        ).status_code == 404
        listing = await c.get("/api/v1/connections/members", headers=admin)
    assert listing.json() == []
    async with app_session(theirs) as db:
        assert await db.get(m.OAuthConnection, conn) is not None
        assert await db.get(m.Credential, cred) is not None


async def test_revoking_a_personal_credential_deletes_its_secrets(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    cred = await credential_row(app_session, tenant, owner=anna)
    async with app_session(tenant) as db:
        await store_secret(db, tenant_id=tenant, name="cred/pw", value="hunter2", kind="credential")
        row = await db.get(m.Credential, cred)
        assert row is not None
        row.secret_refs = {"password": "cred/pw"}
    async with api() as c:
        r = await c.delete(
            f"/api/v1/connections/credentials/{cred}", headers=headers(tenant, "anna@acme.test")
        )
    assert r.status_code == 204, r.text
    async with app_session(tenant) as db:
        assert await db.get(m.Credential, cred) is None
        assert (
            await db.execute(select(m.Secret).where(m.Secret.tenant_id == tenant))
        ).scalars().all() == []


class _FakeHttp:
    """Stands in for the provider's revoke endpoint; records or fails."""

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.posted: list[dict[str, str]] = []

    def __call__(self) -> _FakeHttp:
        return self

    async def __aenter__(self) -> _FakeHttp:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, _url: str, *, data: dict[str, str]) -> None:
        self.posted.append(data)
        if self.fail:
            raise RuntimeError("provider down")


async def test_a_dead_provider_does_not_block_revoking(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.connections import revoke

    fake = _FakeHttp(fail=True)
    monkeypatch.setattr(revoke, "get_client", fake)
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    # google has a revoke endpoint (microsoft has none), so the call is attempted.
    conn = await oauth_row(app_session, tenant, owner=anna, provider="google")
    await _with_tokens(app_session, tenant, conn)
    async with api() as c:
        r = await c.delete(
            f"/api/v1/connections/oauth/{conn}", headers=headers(tenant, "anna@acme.test")
        )
    assert r.status_code == 204, r.text
    assert fake.posted == [{"token": "rt1"}]
    async with app_session(tenant) as db:
        assert await db.get(m.OAuthConnection, conn) is None
        assert (
            await db.execute(select(m.Secret).where(m.Secret.tenant_id == tenant))
        ).scalars().all() == []


@pytest.mark.parametrize("grant_type", ["client_credentials", "service_account"])
async def test_app_secrets_are_never_posted_to_the_provider(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, grant_type: str
) -> None:
    from oc8.connections import revoke

    fake = _FakeHttp(fail=False)
    monkeypatch.setattr(revoke, "get_client", fake)
    tenant = uuid.uuid4()
    conn = await oauth_row(
        app_session, tenant, owner=None, provider="google", grant_type=grant_type
    )
    await _with_tokens(app_session, tenant, conn)
    async with api() as c:
        r = await c.delete(
            f"/api/v1/connections/oauth/{conn}",
            headers=headers(tenant, "admin@acme.test", "org_admin"),
        )
    assert r.status_code == 204, r.text
    assert fake.posted == []
    async with app_session(tenant) as db:
        assert await db.get(m.OAuthConnection, conn) is None


async def test_organisation_credential_delete_and_in_use(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    free = await credential_row(app_session, tenant, owner=None, name="Free")
    used = await credential_row(app_session, tenant, owner=None, name="Used")
    async with app_session(tenant) as db:
        db.add(
            m.DataSource(
                tenant_id=tenant,
                connector_type="s3",
                name="bucket",
                config={"credential": str(used)},
            )
        )
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        # A plain member cannot even see an organisation credential.
        r = await c.delete(
            f"/api/v1/connections/credentials/{free}", headers=headers(tenant, "anna@acme.test")
        )
        assert r.status_code == 404
        assert (
            await c.delete(f"/api/v1/connections/credentials/{free}", headers=admin)
        ).status_code == 204
        assert (
            await c.delete(f"/api/v1/connections/credentials/{used}", headers=admin)
        ).status_code == 409
    async with app_session(tenant) as db:
        assert await db.get(m.Credential, free) is None
        assert await db.get(m.Credential, used) is not None


async def test_mine_and_organisation_listings_carry_no_token_material(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    mine = await oauth_row(app_session, tenant, owner=anna)
    shared = await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    await _with_tokens(app_session, tenant, mine)
    await _with_tokens(app_session, tenant, shared)
    async with api() as c:
        responses = [
            await c.get("/api/v1/connections/mine", headers=headers(tenant, "anna@acme.test")),
            await c.get(
                "/api/v1/connections/organisation",
                headers=headers(tenant, "admin@acme.test", "org_admin"),
            ),
        ]
    for r in responses:
        assert r.status_code == 200 and r.json()
        for needle in ("at1", "rt1", "oauth/", "secretRef", "accessSecretRef"):
            assert needle not in r.text
