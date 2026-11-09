from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.api.mcp_gateway import _connections
from oc8.api.v1.run import run_to_dto
from oc8.authz.pdp import effective_tool_policies
from oc8.connections.access import ConnectionRequired
from oc8.connections.runtime_env import integration_connections, run_mcp_env
from oc8.oauth.tokens import access_ref
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row, member, oauth_row
from tests.connections.test_access import MS_CFG, ODOO_CFG

pytestmark = pytest.mark.asyncio

FRAME = {
    "tools": {
        "microsoft365": {"enabled": True, "read": True, "modify": True},
        "odoo": {"enabled": True, "read": True, "modify": True},
    }
}


async def _world(
    app_session: AppSessionFactory,
    *,
    name: str = "microsoft365",
    cfg: dict[str, Any] = MS_CFG,
    department: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame=FRAME)
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Mira")
        db.add(agent)
        await db.flush()
        conn = m.McpConnection(
            tenant_id=tenant,
            name=name,
            server_url="",
            transport="stdio",
            connected=department,
            config=dict(cfg),
            department_id=dept.id if department else None,
        )
        db.add(conn)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="manual",
            context={"originating_operator": "anna@acme.test"},
        )
        db.add(run)
        await db.flush()
        return tenant, anna, conn.id, run.id


async def _token(
    app_session: AppSessionFactory, tenant: uuid.UUID, conn_id: uuid.UUID, value: str
) -> None:
    async with app_session(tenant) as db:
        conn = await db.get(m.OAuthConnection, conn_id)
        assert conn is not None
        await store_secret(
            db, tenant_id=tenant, name=access_ref(conn_id), value=value, kind="oauth_token"
        )
        conn.access_secret_ref = access_ref(conn_id)
        conn.expires_at = datetime.now(tz=UTC) + timedelta(hours=1)


async def test_a_persons_token_is_injected_and_the_call_audited(
    app_session: AppSessionFactory,
) -> None:
    tenant, anna, conn_id, run_id = await _world(app_session)
    mine = await oauth_row(app_session, tenant, owner=anna, label="anna@contoso.com")
    await _token(app_session, tenant, mine, "annas-token")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        launch = await run_mcp_env(db, run=run, conn=conn, tool="mail_send")
        events = (
            (await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == "connection.used")))
            .scalars()
            .all()
        )
    assert launch.env["GRAPH_ACCESS_TOKEN"] == "annas-token"
    assert (launch.env["OC8_OAUTH_GRANT"], launch.env["OC8_OAUTH_ACCOUNT"]) == (
        "authorization_code",
        "anna@contoso.com",
    )
    assert launch.reusable is False  # never a pooled bridge shared between two people
    assert [
        (e.resource["on_behalf_of"], e.resource["connection_id"], e.resource["access"])
        for e in events
    ] == [(str(anna), str(mine), "oauth")]


async def test_no_connection_records_the_call_to_action(app_session: AppSessionFactory) -> None:
    tenant, _anna, conn_id, run_id = await _world(app_session)
    ctx: dict[str, Any] = {}
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        with pytest.raises(ConnectionRequired):
            await run_mcp_env(db, run=run, conn=conn, tool="mail_send", ctx=ctx)
        listed = await run_mcp_env(db, run=run, conn=conn, listing=True)
    assert "GRAPH_ACCESS_TOKEN" not in listed.env  # tools stay listed; the first call shows the CTA
    assert ctx["connection_required"]["reason"] == "not_connected"
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        dto = run_to_dto(run)
    assert dto.connection_required is not None
    assert (
        dto.connection_required["integration"],
        dto.connection_required["provider"],
        dto.connection_required["reason"],
    ) == ("microsoft365", "microsoft", "not_connected")


async def test_a_dead_refresh_token_asks_to_reconnect(app_session: AppSessionFactory) -> None:
    tenant, anna, conn_id, run_id = await _world(app_session)
    mine = await oauth_row(app_session, tenant, owner=anna)  # expired, no refresh token stored
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        with pytest.raises(ConnectionRequired) as exc:
            await run_mcp_env(db, run=run, conn=conn, tool="mail_send")
        row = await db.get(m.OAuthConnection, mine)
        assert row is not None and row.status == "needs_reauth"
    assert exc.value.reason == "needs_reauth"


async def test_a_persons_login_is_injected(app_session: AppSessionFactory) -> None:
    tenant, anna, conn_id, run_id = await _world(app_session, name="odoo", cfg=ODOO_CFG)
    mine = await credential_row(app_session, tenant, owner=anna)
    async with app_session(tenant) as db:
        await store_secret(
            db, tenant_id=tenant, name=f"credential/{mine}/password", value="pw", kind="credential"
        )
        cred = await db.get(m.Credential, mine)
        assert cred is not None
        cred.secret_refs = {"password": f"credential/{mine}/password"}
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        db.add(
            m.AgentToolAccess(tenant_id=tenant, agent_id=run.agent_id, tool_key="odoo", mode="user")
        )
        await db.flush()
        conn = await db.get(m.McpConnection, conn_id)
        assert conn is not None
        launch = await run_mcp_env(db, run=run, conn=conn, tool="search_records")
    assert (launch.env["ODOO_URL"], launch.env["ODOO_USER"], launch.env["ODOO_PASSWORD"]) == (
        "https://odoo.acme.test",
        "anna",
        "pw",
    )
    assert launch.reusable is False


async def test_a_departments_own_setup_stays_pooled(app_session: AppSessionFactory) -> None:
    cfg = {**ODOO_CFG, "env": {"ODOO_URL": "https://erp"}, "secret_env": {}}
    tenant, _anna, conn_id, run_id = await _world(
        app_session, name="odoo", cfg=cfg, department=True
    )
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        launch = await run_mcp_env(db, run=run, conn=conn)
    assert (launch.env["ODOO_URL"], launch.reusable) == ("https://erp", True)


async def test_an_unbound_oauth_integration_joins_the_run(app_session: AppSessionFactory) -> None:
    tenant, _anna, conn_id, run_id = await _world(app_session)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        agent = await db.get(m.Agent, run.agent_id)
        assert agent is not None
        dept = await db.get(m.Department, agent.department_id)
        conns = await _connections(db, run, dept)
        policies = effective_tool_policies({"tools": {"microsoft365": {"enabled": False}}}, {})
        none = await integration_connections(db, agent=agent, policies=policies, have=set())
    assert [c.id for c in conns] == [conn_id]
    assert none == []
