"""Every surface that existed before connections stays organisation-only: a
personal credential or connection is invisible there and cannot be changed,
tested, deleted, picked by a setup form or fed into a knowledge base."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.credentials.service import (
    CredentialNotFound,
    get_credential,
    list_credentials,
    resolve_credential_field,
)
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, member, oauth_row

pytestmark = pytest.mark.asyncio


async def test_the_credentials_api_never_shows_a_personal_credential(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    org = await credential_row(app_session, tenant, owner=None, name="Odoo system")
    mine = await credential_row(app_session, tenant, owner=anna, name="Odoo")
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        listed = await c.get("/api/v1/credentials", headers=admin)
        assert [row["id"] for row in listed.json()] == [str(org)]
        assert (
            await c.patch(f"/api/v1/credentials/{mine}", json={"name": "x"}, headers=admin)
        ).status_code == 404
        assert (await c.post(f"/api/v1/credentials/{mine}/test", headers=admin)).status_code == 404
        assert (await c.delete(f"/api/v1/credentials/{mine}", headers=admin)).status_code == 404


async def test_setup_forms_cannot_resolve_a_personal_credential(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    mine = await credential_row(app_session, tenant, owner=anna)
    async with app_session(tenant) as db:
        with pytest.raises(CredentialNotFound):
            await get_credential(db, tenant_id=tenant, credential_id=mine, owner="organisation")
        with pytest.raises(CredentialNotFound):
            await resolve_credential_field(
                db, tenant_id=tenant, credential_id=mine, field_key="url", owner="organisation"
            )
        assert (
            await get_credential(db, tenant_id=tenant, credential_id=mine, owner=anna)
        ).id == mine
        assert [
            c.id for c in await list_credentials(db, tenant_id=tenant, owner="organisation")
        ] == []
        assert [c.id for c in await list_credentials(db, tenant_id=tenant, owner=anna)] == [mine]


async def test_the_oauth_api_never_shows_or_deletes_a_personal_connection(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    org = await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    mine = await oauth_row(app_session, tenant, owner=anna)
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        listed = await c.get("/api/v1/oauth/connections", headers=admin)
        assert [row["id"] for row in listed.json()] == [str(org)]
        assert (
            await c.delete(f"/api/v1/oauth/connections/{mine}", headers=admin)
        ).status_code == 404


async def test_a_knowledge_base_cannot_read_through_a_personal_connection(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    mine = await oauth_row(app_session, tenant, owner=anna, provider="google")
    async with api() as c:
        r = await c.post(
            "/api/v1/knowledge/sources",
            json={
                "connectorType": "google_workspace_files",
                "name": "Anna's drive",
                "config": {},
                "kbId": str(uuid.uuid4()),
                "oauthConnectionId": str(mine),
            },
            headers=headers(tenant, "admin@acme.test", "org_admin"),
        )
    assert r.status_code == 400, r.text
    assert "personal connection" in r.text


async def test_an_mcp_login_cannot_reuse_a_personal_credential(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    mine = await credential_row(app_session, tenant, owner=anna, credential_type="smtp_server")
    async with api() as c:
        r = await c.post(
            "/api/v1/mcp/logins",
            json={"name": "odoo", "credentialType": "smtp_server", "credentialId": str(mine)},
            headers=headers(tenant, "admin@acme.test", "org_admin"),
        )
    assert r.status_code == 404, r.text


async def test_the_smtp_picker_cannot_activate_a_personal_credential(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    mine = await credential_row(app_session, tenant, owner=anna, credential_type="smtp_server")
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="Acme"))
    async with api() as c:
        r = await c.put(
            "/api/v1/settings/organization",
            json={"name": "Acme", "region": "eu-1", "active_smtp_credential_id": str(mine)},
            headers=headers(tenant, "admin@acme.test", "org_admin"),
        )
    assert r.status_code == 404, r.text
    assert "smtp_server credential not found" in r.text


async def test_an_mcp_launch_never_mints_a_token_for_a_personal_connection(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    mine = await oauth_row(app_session, tenant, owner=anna)
    cfg = {"secret_env": {"TOKEN": "oauth:self"}, "oauth_connection_id": str(mine)}
    async with app_session(tenant) as db:
        with pytest.raises(RuntimeError, match="personal connection"):
            await resolve_mcp_env(db, tenant_id=tenant, cfg=cfg)
