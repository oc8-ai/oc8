"""A department setup (the plugin's setup form) adopts the plugin's unbound
connection. Agents in every OTHER department must still reach the
integration: in the user's context and through an organisation login bound
by an administrator -- through the real setup path, not a hand-built row."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.api.mcp_gateway import _connections
from oc8.connections.access import ConnectionRequired
from oc8.connections.runtime_env import run_mcp_env
from oc8.credentials.service import create_credential
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, install, member

pytestmark = pytest.mark.asyncio

FRAME = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}


async def _tenant_with_sales_setup(
    app_session: AppSessionFactory,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Odoo installed and set up for Sales through the setup form. Returns
    tenant, Anna, the Marketing department and the organisation login."""
    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    anna = await member(app_session, tenant, "anna@acme.test")
    async with api() as c:
        capa_id = await install(c, admin, "odoo_mcp")
        async with app_session(tenant) as db:
            sales = m.Department(tenant_id=tenant, name="Sales", frame=FRAME)
            marketing = m.Department(tenant_id=tenant, name="Marketing", frame=FRAME)
            db.add_all([sales, marketing])
            await db.flush()
            org = await create_credential(
                db,
                tenant_id=tenant,
                name="Company Odoo",
                credential_type="odoo_login",
                field_values={
                    "url": "https://erp.acme.test",
                    "database": "acme",
                    "username": "sales-bot",
                    "password": "org-pw",
                },
            )
            sales_id, marketing_id, org_id = sales.id, marketing.id, org.id
        r = await c.post(
            f"/api/v1/capas/{capa_id}/setup",
            json={"values": {"department": str(sales_id), "system": str(org_id)}},
            headers=admin,
        )
        assert r.status_code == 200, r.text
    async with app_session(tenant) as db:
        rows = (await db.execute(m.McpConnection.__table__.select())).all()
        # The setup adopted the only materialised row: no unbound one is left.
        assert [row.department_id for row in rows if row.name == "odoo"] == [sales_id]
    return tenant, anna, marketing_id, org_id


async def _marketing_run(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    marketing: uuid.UUID,
    *,
    context: dict[str, Any],
    access: dict[str, Any],
) -> uuid.UUID:
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=marketing, name="Mia")
        db.add(agent)
        await db.flush()
        db.add(m.AgentToolAccess(tenant_id=tenant, agent_id=agent.id, tool_key="odoo", **access))
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="manual", context=context)
        db.add(run)
        await db.flush()
        return run.id


async def _launch(app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID) -> Any:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        agent = await db.get(m.Agent, run.agent_id)
        assert agent is not None
        dept = await db.get(m.Department, agent.department_id)
        conns = [c for c in await _connections(db, run, dept) if c.name == "odoo"]
        assert len(conns) == 1, "the Marketing agent gets the Odoo tools"
        assert conns[0].department_id is None  # never Sales' own row
        return await run_mcp_env(db, run=run, conn=conns[0], tool="search_records")


async def test_another_department_acts_in_the_users_context(
    app_session: AppSessionFactory,
) -> None:
    tenant, anna, marketing, _org = await _tenant_with_sales_setup(app_session)
    mine = await credential_row(app_session, tenant, owner=anna)
    async with app_session(tenant) as db:
        await store_secret(
            db, tenant_id=tenant, name=f"credential/{mine}/pw", value="anna-pw", kind="credential"
        )
        cred = await db.get(m.Credential, mine)
        assert cred is not None
        cred.secret_refs = {"password": f"credential/{mine}/pw"}
    run_id = await _marketing_run(
        app_session,
        tenant,
        marketing,
        context={"originating_operator": "anna@acme.test"},
        access={"mode": "user"},
    )
    launch = await _launch(app_session, tenant, run_id)
    assert (launch.env["ODOO_USER"], launch.env["ODOO_PASSWORD"]) == ("anna", "anna-pw")
    assert not {"sales-bot", "org-pw"} & set(launch.env.values())  # no mixed identities
    assert launch.reusable is False  # a person's login is never pooled


async def test_another_department_uses_an_organisation_login_bound_by_an_admin(
    app_session: AppSessionFactory,
) -> None:
    tenant, _anna, marketing, org = await _tenant_with_sales_setup(app_session)
    run_id = await _marketing_run(
        app_session,
        tenant,
        marketing,
        context={},  # an unattended run: nobody is working with it
        access={"mode": "fixed", "credential_id": org, "set_with_manage": True},
    )
    launch = await _launch(app_session, tenant, run_id)
    assert (launch.env["ODOO_USER"], launch.env["ODOO_PASSWORD"]) == ("sales-bot", "org-pw")


async def _second_org_login(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    """Two organisation logins: no single one is everybody's default."""
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Other Odoo",
            credential_type="odoo_login",
            field_values={"url": "https://other.acme.test", "username": "x", "password": "y"},
        )


SALES_VALUES = {"https://erp.acme.test", "acme", "sales-bot", "org-pw"}


async def test_another_department_without_access_gets_nothing_of_the_setup(
    app_session: AppSessionFactory,
) -> None:
    tenant, _anna, marketing, _org = await _tenant_with_sales_setup(app_session)
    await _second_org_login(app_session, tenant)
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=marketing, name="Mia")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="manual",
            context={"originating_operator": "anna@acme.test"},
        )
        db.add(run)
        await db.flush()
        sales_row = await db.scalar(
            select(m.McpConnection).where(
                m.McpConnection.name == "odoo", m.McpConnection.department_id.is_not(None)
            )
        )
        assert sales_row is not None
        template = m.McpConnection(
            tenant_id=tenant,
            name="odoo",
            server_url="",
            transport="stdio",
            config={**sales_row.config},
            connected=False,
        )
        # Whichever row the run is given -- Sales' own (a pinned run) or a
        # copy of it without a department -- Sales' login is never used.
        for conn in (sales_row, template):
            with pytest.raises(ConnectionRequired) as exc:
                await run_mcp_env(db, run=run, conn=conn, tool="search_records")
            assert exc.value.reason == "not_configured"
            listed = await run_mcp_env(db, run=run, conn=conn, listing=True)
            assert not SALES_VALUES & set(listed.env.values())


async def test_a_bridge_that_only_lists_tools_gets_only_what_the_plugin_declares(
    app_session: AppSessionFactory,
) -> None:
    """An allowlist, not a filter: whatever a stored config holds -- odd
    names, lower case, delegated or credential references in values, nested
    maps -- the listing bridge sees the manifest's plain env and nothing else."""
    from oc8.capas.discovery import resolve_tool_pack_connection

    tenant, _anna, marketing, _org = await _tenant_with_sales_setup(app_session)
    await _second_org_login(app_session, tenant)
    async with app_session(tenant) as db:
        await store_secret(db, tenant_id=tenant, name="dept/hidden", value="hidden-pw", kind="x")
        agent = m.Agent(tenant_id=tenant, department_id=marketing, name="Mia")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="manual", context={})
        db.add(run)
        sales_row = await db.scalar(
            select(m.McpConnection).where(
                m.McpConnection.name == "odoo", m.McpConnection.department_id.is_not(None)
            )
        )
        assert sales_row is not None
        cfg = dict(sales_row.config)
        cfg["env"] = {
            **cfg.get("env", {}),
            "odoo_password": "plain-dept-pw",
            "SOME_LOGIN": "credential/hidden",
            "NESTED": {"token": "nested-dept-pw"},
        }
        cfg["secret_env"] = {
            **cfg.get("secret_env", {}),
            "x_token": "dept/hidden",
            "Graph": "oauth-delegated:boss@acme.test",
        }
        cfg["auth_header_name"] = "odoo_password"
        sales_row.config = cfg
        await db.flush()
        listed = await run_mcp_env(db, run=run, conn=sales_row, listing=True)
    declared = resolve_tool_pack_connection("odoo_mcp", "primary")
    assert declared is not None
    assert listed.env == {
        k: v for k, v in dict(declared.config.get("env") or {}).items() if isinstance(v, str)
    }
    assert not (SALES_VALUES | {"plain-dept-pw", "hidden-pw", "nested-dept-pw"}) & set(
        str(v) for v in listed.env.values()
    )
