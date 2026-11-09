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
from oc8.connections.access import ConnectionRequired, binding_fingerprint
from oc8.connections.runtime_env import integration_connections, run_mcp_env
from oc8.oauth.tokens import access_ref
from oc8.secrets.service import store_secret
from tests.audit.helpers import flush_audit
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row, enable_plugin_row, member, oauth_row
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
    await enable_plugin_row(app_session, tenant, str(cfg["_plugin_name"]))
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
        await flush_audit(db)  # staged audit events reach the chain at commit
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


async def test_a_persons_login_replaces_the_departments_identity_whole(
    app_session: AppSessionFactory,
) -> None:
    """A person's login never mixes with the department's: every identity
    variable the department stored is dropped, even one the person's own
    login leaves empty (here: no password, no database)."""
    cfg = {
        **ODOO_CFG,
        "env": {
            "ODOO_URL": "https://dept-erp",
            "ODOO_DB": "dept-db",
            "ODOO_USER": "dept-bot",
            "PYTHONPATH": "/app/capas/odoo_mcp",
        },
        "secret_env": {"ODOO_PASSWORD": "dept/pw", "ODOO_API_KEY": "dept/key"},
    }
    tenant, anna, conn_id, run_id = await _world(app_session, name="odoo", cfg=cfg, department=True)
    await credential_row(app_session, tenant, owner=anna)  # url + username only
    async with app_session(tenant) as db:
        await store_secret(db, tenant_id=tenant, name="dept/pw", value="dept-pw", kind="credential")
        await store_secret(
            db, tenant_id=tenant, name="dept/key", value="dept-key", kind="credential"
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        db.add(
            m.AgentToolAccess(tenant_id=tenant, agent_id=run.agent_id, tool_key="odoo", mode="user")
        )
        await db.flush()
        conn = await db.get(m.McpConnection, conn_id)
        assert conn is not None
        launch = await run_mcp_env(db, run=run, conn=conn, tool="search_records")
    assert (launch.env["ODOO_URL"], launch.env["ODOO_USER"]) == ("https://odoo.acme.test", "anna")
    for name in ("ODOO_DB", "ODOO_PASSWORD", "ODOO_API_KEY"):
        assert name not in launch.env
    assert not {"dept-pw", "dept-key", "dept-db", "dept-bot", "https://dept-erp"} & set(
        launch.env.values()
    )
    assert launch.env["PYTHONPATH"] == "/app/capas/odoo_mcp"  # not identity: kept


async def test_a_later_successful_call_clears_the_call_to_action(
    app_session: AppSessionFactory,
) -> None:
    tenant, anna, conn_id, run_id = await _world(app_session)
    ctx: dict[str, Any] = {}
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        with pytest.raises(ConnectionRequired):
            await run_mcp_env(db, run=run, conn=conn, tool="mail_send", ctx=ctx)
    assert ctx["connection_required"] is not None
    mine = await oauth_row(app_session, tenant, owner=anna)  # the person connects
    await _token(app_session, tenant, mine, "annas-token")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        await run_mcp_env(db, run=run, conn=conn, tool="mail_send", ctx=ctx)
    assert ctx["connection_required"] is None
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run_to_dto(run).connection_required is None


async def test_another_integrations_success_keeps_the_call_to_action(
    app_session: AppSessionFactory,
) -> None:
    tenant, anna, conn_id, run_id = await _world(app_session)
    other = {
        "integration": "odoo_mcp",
        "provider": None,
        "label": "Odoo",
        "reason": "not_connected",
    }
    mine = await oauth_row(app_session, tenant, owner=anna)
    await _token(app_session, tenant, mine, "annas-token")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "connection_required": other}
        await db.flush()
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        await run_mcp_env(db, run=run, conn=conn, tool="mail_send")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run_to_dto(run).connection_required == other


async def test_the_in_process_engine_audits_each_session_it_opens(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.agent import engine

    async def fake_open(**kw: Any) -> dict[str, Any]:
        return kw

    monkeypatch.setattr(engine, "open_tool_session", fake_open)
    tenant, anna, conn_id, run_id = await _world(app_session)
    mine = await oauth_row(app_session, tenant, owner=anna)
    await _token(app_session, tenant, mine, "annas-token")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        opened = await engine._open_mcp_session(db, tenant_id=tenant, mcp_conn=conn, run=run)
        await flush_audit(db)  # staged audit events reach the chain at commit
        events = (
            (await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == "connection.used")))
            .scalars()
            .all()
        )
    assert opened["env"]["GRAPH_ACCESS_TOKEN"] == "annas-token"
    assert [
        (e.resource["tool"], e.resource["on_behalf_of"], e.resource["connection_id"])
        for e in events
    ] == [("*", str(anna), str(mine))]


@pytest.mark.parametrize("status", ["disabled", "installed", None])
async def test_a_plugin_that_is_not_enabled_acts_through_nobody(
    app_session: AppSessionFactory, status: str | None
) -> None:
    """Disabling a plugin keeps its rows (there is no uninstall), so the
    runtime itself refuses: the person's own token is not used either."""
    tenant, anna, conn_id, run_id = await _world(app_session)
    mine = await oauth_row(app_session, tenant, owner=anna)
    await _token(app_session, tenant, mine, "annas-token")
    async with app_session(tenant) as db:
        inst = (
            await db.execute(
                select(m.CapaInstallation)
                .join(m.Capa, m.Capa.id == m.CapaInstallation.capa_id)
                .where(m.Capa.name == "microsoft365")
            )
        ).scalar_one()
        if status is None:
            await db.delete(inst)
        else:
            inst.status = status
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        with pytest.raises(ConnectionRequired) as exc:
            await run_mcp_env(db, run=run, conn=conn, tool="mail_send")
    assert exc.value.reason == "not_configured"


async def _disable(app_session: AppSessionFactory, tenant: uuid.UUID, plugin: str) -> None:
    async with app_session(tenant) as db:
        inst = (
            await db.execute(
                select(m.CapaInstallation)
                .join(m.Capa, m.Capa.id == m.CapaInstallation.capa_id)
                .where(m.Capa.name == plugin)
            )
        ).scalar_one()
        inst.status = "disabled"


async def test_a_disabled_plugin_is_refused_on_every_launch_path(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing, a call, a fixed binding to the person's own account, a deleted
    agent, the in-process engine (with and without a run) and read_resource
    all stop at the gate -- before a token is minted."""
    from oc8.agent import engine
    from oc8.agent.control_tools import _execute_read_resource
    from oc8.modelrouter.types import ToolCall

    minted: list[str] = []

    async def no_launch(**kw: Any) -> None:
        minted.append("session")

    monkeypatch.setattr(engine, "open_tool_session", no_launch)
    tenant, anna, conn_id, run_id = await _world(app_session, department=True)
    mine = await oauth_row(app_session, tenant, owner=anna)
    await _token(app_session, tenant, mine, "annas-token")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        agent = await db.get(m.Agent, run.agent_id)
        assert agent is not None
        db.add(
            m.AgentToolAccess(
                tenant_id=tenant,
                agent_id=agent.id,
                tool_key="microsoft365",
                mode="fixed",
                oauth_connection_id=mine,
                set_by_member_id=anna,
                binding_fingerprint=await binding_fingerprint(
                    db, agent, "microsoft365", version_id=None
                ),
            )
        )
    await _disable(app_session, tenant, "microsoft365")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        conn = await db.get(m.McpConnection, conn_id)
        assert run is not None and conn is not None
        agent = await db.get(m.Agent, run.agent_id)
        assert agent is not None
        for listing in (True, False):
            with pytest.raises(ConnectionRequired) as exc:
                await run_mcp_env(db, run=run, conn=conn, tool="mail_send", listing=listing)
            assert exc.value.reason == "not_configured"
        for with_run in (run, None):
            with pytest.raises(ConnectionRequired):
                await engine._open_mcp_session(db, tenant_id=tenant, mcp_conn=conn, run=with_run)
        for rid in (run.id, None):
            out = await _execute_read_resource(
                db,
                tenant_id=tenant,
                agent=agent,
                tc=ToolCall(id="1", name="read_resource", arguments={"connection": conn.name}),
                run_id=rid,
            )
            assert "connection_required" in out.output
        assert run.context["connection_required"]["reason"] == "not_configured"
        await db.delete(agent)
        await db.flush()
        with pytest.raises(ConnectionRequired):
            await run_mcp_env(db, run=run, conn=conn, tool="mail_send")
    assert minted == []
