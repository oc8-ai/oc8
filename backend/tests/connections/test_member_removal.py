from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, member, oauth_row

pytestmark = pytest.mark.asyncio


async def test_removing_a_member_purges_their_connections(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    conn = await oauth_row(app_session, tenant, owner=anna)
    cred = await credential_row(app_session, tenant, owner=anna)
    org = await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    async with app_session(tenant) as db:
        await store_secret(
            db, tenant_id=tenant, name=f"credential/{cred}/password", value="pw", kind="credential"
        )
        row = await db.get(m.Credential, cred)
        assert row is not None
        row.secret_refs = {"password": f"credential/{cred}/password"}
    async with api() as c:
        r = await c.delete(
            f"/api/v1/members/{anna}", headers=headers(tenant, "admin@acme.test", "org_admin")
        )
    assert r.status_code == 204, r.text
    async with app_session(tenant) as db:
        assert await db.get(m.OAuthConnection, conn) is None
        assert await db.get(m.Credential, cred) is None
        assert await db.get(m.OAuthConnection, org) is not None
        secrets = (
            (await db.execute(select(m.Secret.name).where(m.Secret.tenant_id == tenant)))
            .scalars()
            .all()
        )
        assert f"credential/{cred}/password" not in secrets
