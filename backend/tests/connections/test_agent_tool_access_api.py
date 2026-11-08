from __future__ import annotations

import pytest
from sqlalchemy import select

from oc8 import models as m
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
            json={"mode": "fixed", "oauthConnectionId": str(annas), "confirmShare": True},
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
        ("oauth", "me", "ben@acme.test"),
    ]
    assert ("oauth", "member", "anna@acme.test") in [
        (o["kind"], o["owner"], o["label"]) for o in for_admin
    ]


ADMIN = "org_admin"
MANAGER = "dept_manager"


async def _events(app_session: AppSessionFactory, tenant, action: str) -> list[dict]:  # type: ignore[type-arg]
    async with app_session(tenant) as db:
        rows = (
            await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == action))
        ).scalars()
        return [dict(r.resource) for r in rows]


async def _row(app_session: AppSessionFactory, agent: m.Agent) -> m.AgentToolAccess:
    async with app_session(agent.tenant_id) as db:
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent.id)
        )
        assert row is not None
        return row


async def test_binding_your_own_account_needs_confirm_share_and_is_audited(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    annas = await oauth_row(app_session, tenant, owner=anna)
    async with api() as c:
        await install(c, headers(tenant, "root@acme.test", ADMIN), "microsoft365")
        url = f"/api/v1/agents/{agent.id}/tool-access/microsoft365"
        me = headers(tenant, "anna@acme.test", MANAGER)
        body = {"mode": "fixed", "oauthConnectionId": str(annas)}
        assert (await c.put(url, json=body, headers=me)).status_code == 422
        ok = await c.put(url, json={**body, "confirmShare": True}, headers=me)
        assert ok.status_code == 200, ok.text
    row = await _row(app_session, agent)
    assert (row.set_by_member_id, row.set_with_manage) == (anna, False)
    assert row.agent_revision is not None
    event = (await _events(app_session, tenant, "agent.tool_access.updated"))[-1]
    assert event["target_owner_member_id"] == str(anna)
    assert event["consent"] == "owner" and event["confirm_share"] is True
    assert event["target_label"] == "anna@acme.test"
    assert event["previous"] == {"mode": None, "oauth_connection_id": None, "credential_id": None}


async def test_an_administrator_may_bind_another_persons_account(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    root = await member(app_session, tenant, "root@acme.test", name="Root")
    annas = await oauth_row(app_session, tenant, owner=anna)
    async with api() as c:
        await install(c, headers(tenant, "root@acme.test", ADMIN), "microsoft365")
        r = await c.put(
            f"/api/v1/agents/{agent.id}/tool-access/microsoft365",
            json={"mode": "fixed", "oauthConnectionId": str(annas)},
            headers=headers(tenant, "root@acme.test", ADMIN),
        )
        assert r.status_code == 200, r.text
    row = await _row(app_session, agent)
    assert (row.set_by_member_id, row.set_with_manage) == (root, True)
    event = (await _events(app_session, tenant, "agent.tool_access.updated"))[-1]
    assert (event["consent"], event["target_owner_member_id"]) == ("manage", str(anna))


async def test_organisation_targets_need_the_manage_right_except_the_departments_own(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    await member(app_session, tenant, "ben@acme.test", name="Ben")
    shared = await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    dept_owned = await oauth_row(app_session, tenant, owner=None, label="dept@acme.test")
    async with api() as c:
        await install(c, headers(tenant, "root@acme.test", ADMIN), "microsoft365")
        url = f"/api/v1/agents/{agent.id}/tool-access/microsoft365"
        ben = headers(tenant, "ben@acme.test", MANAGER)
        refused = await c.put(
            url, json={"mode": "fixed", "oauthConnectionId": str(shared)}, headers=ben
        )
        assert refused.status_code == 403, refused.text
        admin = await c.put(
            url,
            json={"mode": "fixed", "oauthConnectionId": str(shared)},
            headers=headers(tenant, "root@acme.test", ADMIN),
        )
        assert admin.status_code == 200, admin.text
        async with app_session(tenant) as db:
            conn = (
                (
                    await db.execute(
                        select(m.McpConnection).where(m.McpConnection.name == "microsoft365")
                    )
                )
                .scalars()
                .first()
            )
            if conn is None:
                conn = m.McpConnection(
                    tenant_id=tenant, name="microsoft365", server_url="", transport="stdio"
                )
                db.add(conn)
            conn.department_id = agent.department_id
            conn.config = {**dict(conn.config or {}), "oauth_connection_id": str(dept_owned)}
        options = (await c.get(url + "/options", headers=ben)).json()
        assert [(o["owner"], o["label"]) for o in options if o["owner"] == "organisation"] == [
            ("organisation", "dept@acme.test")
        ]
        exception = await c.put(
            url, json={"mode": "fixed", "oauthConnectionId": str(dept_owned)}, headers=ben
        )
        assert exception.status_code == 200, exception.text
        # Non-admins may still go back to the default or choose user mode.
        assert (await c.put(url, json={"mode": "user"}, headers=ben)).status_code == 200
        assert (await c.delete(url, headers=ben)).status_code == 204


async def test_a_fixed_login_binding_and_input_rules(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    org_odoo = await credential_row(app_session, tenant, owner=None, name="Odoo system")
    async with api() as c:
        admin = headers(tenant, "root@acme.test", ADMIN)
        await install(c, admin, "odoo_mcp")
        url = f"/api/v1/agents/{agent.id}/tool-access/odoo"
        ok = await c.put(url, json={"mode": "fixed", "credentialId": str(org_odoo)}, headers=admin)
        assert ok.status_code == 200, ok.text
        assert (ok.json()["credentialId"], ok.json()["targetLabel"]) == (
            str(org_odoo),
            "Odoo system",
        )
        extra = await c.put(
            url, json={"mode": "user", "credentialId": str(org_odoo)}, headers=admin
        )
        assert extra.status_code == 422
        unknown = await c.delete(f"/api/v1/agents/{agent.id}/tool-access/nothing", headers=admin)
        assert unknown.status_code == 404


async def test_other_tenants_ids_are_not_found_and_nothing_secret_is_returned(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    other = await _make_agent(app_session)
    tenant = agent.tenant_id
    theirs = await oauth_row(app_session, other.tenant_id, owner=None)
    mine = await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    async with api() as c:
        admin = headers(tenant, "root@acme.test", ADMIN)
        await install(c, admin, "microsoft365")
        cross_agent = await c.get(f"/api/v1/agents/{other.id}/tool-access", headers=admin)
        assert cross_agent.status_code == 404
        cross_target = await c.put(
            f"/api/v1/agents/{agent.id}/tool-access/microsoft365",
            json={"mode": "fixed", "oauthConnectionId": str(theirs)},
            headers=admin,
        )
        assert cross_target.status_code == 422
        await c.put(
            f"/api/v1/agents/{agent.id}/tool-access/microsoft365",
            json={"mode": "fixed", "oauthConnectionId": str(mine)},
            headers=admin,
        )
        listing = await c.get(f"/api/v1/agents/{agent.id}/tool-access", headers=admin)
        options = await c.get(
            f"/api/v1/agents/{agent.id}/tool-access/microsoft365/options", headers=admin
        )
    for text in (listing.text.lower(), options.text.lower()):
        assert "secret" not in text and "token" not in text
