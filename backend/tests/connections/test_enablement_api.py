"""Enabling an organisation login for departments on the Connections page."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, install, member

pytestmark = pytest.mark.asyncio


async def _department(app_session: AppSessionFactory, tenant: uuid.UUID, name: str) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name=name)
        db.add(dept)
        await db.flush()
        return dept.id


async def test_put_departments_returns_the_enablement(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    cred = await credential_row(app_session, tenant, owner=None)
    sales = await _department(app_session, tenant, "Sales")
    support = await _department(app_session, tenant, "Support")
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=sales, name="Ada")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
        db.add(
            m.AgentToolAccess(
                tenant_id=tenant,
                agent_id=agent_id,
                tool_key="odoo",
                mode="fixed",
                credential_id=cred,
            )
        )
    admin = headers(tenant, "admin@acme.test", "org_admin")
    url = f"/api/v1/connections/credentials/{cred}/departments"
    async with api() as c:
        r = await c.put(
            url,
            json={"departmentIds": [str(sales), str(support)], "allDepartments": False},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert sorted(body["departmentIds"]) == sorted([str(sales), str(support)])
        assert body["allDepartments"] is False
        assert sorted(body["usedBy"]["departments"]) == sorted([str(sales), str(support)])
        assert body["usedBy"]["agents"] == [str(agent_id)]
        assert isinstance(body["incomplete"], list)

        r = await c.put(url, json={"departmentIds": [], "allDepartments": True}, headers=admin)
        assert r.status_code == 200, r.text
        assert r.json()["departmentIds"] == []
        assert r.json()["allDepartments"] is True

        # A plain member may not enable the organisation's login.
        r = await c.put(
            url,
            json={"departmentIds": [str(sales)], "allDepartments": False},
            headers=headers(tenant, "anna@acme.test"),
        )
        assert r.status_code == 403

        # An unknown department is refused.
        r = await c.put(
            url, json={"departmentIds": [str(uuid.uuid4())], "allDepartments": False}, headers=admin
        )
        assert r.status_code == 422

    async with app_session(tenant) as db:
        row = await db.get(m.Credential, cred)
        assert row is not None and row.all_departments is True


async def test_a_personal_login_is_not_enabled_for_departments(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    cred = await credential_row(app_session, tenant, owner=anna)
    sales = await _department(app_session, tenant, "Sales")
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        r = await c.put(
            f"/api/v1/connections/credentials/{cred}/departments",
            json={"departmentIds": [str(sales)], "allDepartments": False},
            headers=admin,
        )
    assert r.status_code == 404


async def test_creating_an_incomplete_login_names_what_is_missing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    await member(app_session, tenant, "anna@acme.test")
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        await install(c, admin, "odoo_mcp")
        r = await c.post(
            "/api/v1/connections/credentials",
            json={
                "integration": "odoo_mcp",
                "owner": "organisation",
                "fieldValues": {"url": "", "password": "pw"},
            },
            headers=admin,
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"code": "credential_incomplete", "missing": ["url"]}


async def test_settings_credential_list_shows_what_uses_a_login(
    app_session: AppSessionFactory,
) -> None:
    """The login picker of a setup lists through `/credentials`: it shows the
    same enablement, missing fields and users as the Connections page."""
    tenant = uuid.uuid4()
    cred = await credential_row(app_session, tenant, owner=None)
    sales = await _department(app_session, tenant, "Sales")
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=sales, name="Ada")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
        db.add(
            m.AgentToolAccess(
                tenant_id=tenant,
                agent_id=agent_id,
                tool_key="odoo",
                mode="fixed",
                credential_id=cred,
            )
        )
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        await install(c, admin, "odoo_mcp")
        r = await c.put(
            f"/api/v1/connections/credentials/{cred}/departments",
            json={"departmentIds": [str(sales)], "allDepartments": False},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        r = await c.get("/api/v1/credentials?type=odoo_login", headers=admin)
    assert r.status_code == 200, r.text
    (row,) = r.json()
    assert row["departmentIds"] == [str(sales)]
    assert row["allDepartments"] is False
    assert row["usedBy"] == {"departments": [str(sales)], "agents": [str(agent_id)]}
    assert row["incomplete"] == ["database", "password"]
