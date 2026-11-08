from __future__ import annotations

import pytest

from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, install, member, oauth_row
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio


async def test_binding_another_persons_account_needs_its_owner_or_an_admin(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    await member(app_session, tenant, "ben@acme.test", name="Ben")
    annas = await oauth_row(app_session, tenant, owner=anna)
    org_odoo = await credential_row(app_session, tenant, owner=None, name="Odoo system")
    async with api() as c:
        await install(c, headers(tenant, "root@acme.test", "org_admin"), "microsoft365")
        await install(c, headers(tenant, "root@acme.test", "org_admin"), "odoo_mcp")
        url = f"/api/v1/agents/{agent.id}/tool-access/microsoft365"
        ben = headers(tenant, "ben@acme.test", "dept_manager")
        refused = await c.put(
            url, json={"mode": "fixed", "oauthConnectionId": str(annas)}, headers=ben
        )
        assert refused.status_code == 403, refused.text
        own = await c.put(
            url,
            json={"mode": "fixed", "oauthConnectionId": str(annas)},
            headers=headers(tenant, "anna@acme.test", "dept_manager"),
        )
        assert own.status_code == 200, own.text
        assert (own.json()["mode"], own.json()["targetOwnerName"]) == ("fixed", "Anna")
        wrong = await c.put(
            f"/api/v1/agents/{agent.id}/tool-access/microsoft365",
            json={"mode": "fixed", "credentialId": str(org_odoo)},
            headers=headers(tenant, "root@acme.test", "org_admin"),
        )
        assert wrong.status_code == 422
        reset = await c.delete(url, headers=headers(tenant, "root@acme.test", "org_admin"))
        assert reset.status_code == 204
        rows = (await c.get(f"/api/v1/agents/{agent.id}/tool-access", headers=ben)).json()
    by_key = {row["toolKey"]: row for row in rows}
    assert (by_key["microsoft365"]["mode"], by_key["microsoft365"]["defaultMode"]) == (
        "default",
        "user",
    )
    assert by_key["odoo"]["defaultMode"] == "organisation"


async def test_options_show_other_peoples_accounts_to_admins_only(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    ben = await member(app_session, tenant, "ben@acme.test", name="Ben")
    await oauth_row(app_session, tenant, owner=anna)
    await oauth_row(app_session, tenant, owner=ben, label="ben@acme.test")
    await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    async with api() as c:
        await install(c, headers(tenant, "root@acme.test", "org_admin"), "microsoft365")
        url = f"/api/v1/agents/{agent.id}/tool-access/microsoft365/options"
        for_ben = (
            await c.get(url, headers=headers(tenant, "ben@acme.test", "dept_manager"))
        ).json()
        for_admin = (
            await c.get(url, headers=headers(tenant, "root@acme.test", "org_admin"))
        ).json()
    assert [(o["kind"], o["owner"], o["label"]) for o in for_ben] == [
        ("user", None, "user"),
        ("oauth", "organisation", "shared@acme.test"),
        ("oauth", "me", "ben@acme.test"),
    ]
    assert ("oauth", "member", "anna@acme.test") in [
        (o["kind"], o["owner"], o["label"]) for o in for_admin
    ]
