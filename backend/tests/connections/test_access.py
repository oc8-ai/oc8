"""Which account a tool call acts through. The rules that matter most: user
mode uses the acting person's own connection or nothing; a fixed binding uses
its target or nothing; a department's app-only setup keeps working."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.connections.access import ConnectionRequired, ToolAccess, resolve_tool_access
from oc8.connections.revoke import purge_member_connections
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row, member, oauth_row
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio

MS_CFG: dict[str, Any] = {
    "_plugin_name": "microsoft365",
    "_connection_key": "primary",
    "command": "python",
    "args": ["-m", "mcp_bridge"],
    "env": {"PYTHONPATH": "/app/capas/microsoft365"},
    "secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"},
}
ODOO_CFG: dict[str, Any] = {
    "_plugin_name": "odoo_mcp",
    "_connection_key": "primary",
    "command": "uv",
    "args": ["tool", "run", "mcp-server-odoo"],
    "env": {},
    "secret_env": {},
}


async def _setup(
    app_session: AppSessionFactory,
    *,
    name: str = "microsoft365",
    cfg: dict[str, Any] | None = None,
    department: bool = False,
    context: dict[str, Any] | None = None,
) -> tuple[m.Agent, uuid.UUID, uuid.UUID, uuid.UUID]:
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    async with app_session(agent.tenant_id) as db:
        conn = m.McpConnection(
            tenant_id=agent.tenant_id,
            name=name,
            server_url="",
            transport="stdio",
            connected=department,
            config=dict(cfg if cfg is not None else MS_CFG),
            department_id=agent.department_id if department else None,
        )
        db.add(conn)
        await db.flush()
        run = m.AgentRun(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            source="manual",
            context=context if context is not None else {"originating_operator": "anna@acme.test"},
        )
        db.add(run)
        await db.flush()
        return agent, anna, conn.id, run.id


async def _access(
    app_session: AppSessionFactory, agent: m.Agent, conn_id: uuid.UUID, run_id: uuid.UUID
) -> ToolAccess:
    async with app_session(agent.tenant_id) as db:
        conn = await db.get(m.McpConnection, conn_id)
        run = await db.get(m.AgentRun, run_id)
        live = await db.get(m.Agent, agent.id)
        assert conn is not None and run is not None and live is not None
        return await resolve_tool_access(db, run=run, agent=live, conn=conn)


async def _bind(app_session: AppSessionFactory, agent: m.Agent, **row: Any) -> None:
    async with app_session(agent.tenant_id) as db:
        db.add(m.AgentToolAccess(tenant_id=agent.tenant_id, agent_id=agent.id, **row))


async def test_user_mode_uses_the_acting_persons_own_connection(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _setup(app_session)
    mine = await oauth_row(app_session, agent.tenant_id, owner=anna)
    access = await _access(app_session, agent, conn, run)
    assert (access.kind, access.oauth_connection.id, access.on_behalf_of) == ("oauth", mine, anna)  # type: ignore[union-attr]


async def test_user_mode_never_falls_back_to_another_member_or_the_organisation(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _setup(app_session)
    ben = await member(app_session, agent.tenant_id, "ben@acme.test")
    await oauth_row(app_session, agent.tenant_id, owner=ben, label="ben@acme.test")
    await oauth_row(app_session, agent.tenant_id, owner=None, label="shared@acme.test")
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.as_context() == {
        "integration": "microsoft365",
        "provider": "microsoft",
        "label": exc.value.integration.label,
        "reason": "not_connected",
    }
    assert str(exc.value).startswith("connection_required:")


async def test_an_expired_connection_asks_to_reconnect(app_session: AppSessionFactory) -> None:
    agent, anna, conn, run = await _setup(app_session)
    await oauth_row(app_session, agent.tenant_id, owner=anna, status="needs_reauth")
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "needs_reauth"


async def test_a_fixed_binding_is_used_for_everyone(app_session: AppSessionFactory) -> None:
    agent, anna, conn, _run_id = await _setup(app_session)
    annas = await oauth_row(app_session, agent.tenant_id, owner=anna)
    await _bind(
        app_session,
        agent,
        tool_key="microsoft365",
        mode="fixed",
        oauth_connection_id=annas,
        set_by_member_id=anna,
        agent_revision=await _revision(app_session, agent),
    )
    # A scheduled run nobody is working with still uses the fixed account.
    async with app_session(agent.tenant_id) as db:
        run = m.AgentRun(
            tenant_id=agent.tenant_id, agent_id=agent.id, source="cron", context={"task": "x"}
        )
        db.add(run)
        await db.flush()
        run_id = run.id
    access = await _access(app_session, agent, conn, run_id)
    assert (access.kind, access.oauth_connection.id) == ("oauth", annas)  # type: ignore[union-attr]


async def test_fixed_binding_with_deleted_target_requires_a_connection(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _setup(app_session)
    annas = await oauth_row(app_session, agent.tenant_id, owner=anna)
    await _bind(
        app_session, agent, tool_key="microsoft365", mode="fixed", oauth_connection_id=annas
    )
    await oauth_row(
        app_session, agent.tenant_id, owner=anna, label="second@acme.test"
    )  # would satisfy user mode
    async with app_session(agent.tenant_id) as db:
        target = await db.get(m.OAuthConnection, annas)
        await db.delete(target)
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "not_connected"


async def test_a_departments_app_only_setup_keeps_working(app_session: AppSessionFactory) -> None:
    agent, _anna, conn, run = await _setup(
        app_session, cfg={**MS_CFG, "oauth_connection_id": str(uuid.uuid4())}, department=True
    )
    access = await _access(app_session, agent, conn, run)
    assert access.kind == "legacy"


async def test_the_only_organisation_credential_is_the_default(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _setup(app_session, name="odoo", cfg=ODOO_CFG)
    org = await credential_row(app_session, agent.tenant_id, owner=None, name="Odoo system")
    access = await _access(app_session, agent, conn, run)
    assert (access.kind, access.credential.id) == ("credential", org)  # type: ignore[union-attr]
    await credential_row(app_session, agent.tenant_id, owner=None, name="Odoo second")
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "not_configured"


async def test_user_mode_for_a_login_uses_the_persons_own_credential(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _setup(app_session, name="odoo", cfg=ODOO_CFG)
    await credential_row(app_session, agent.tenant_id, owner=None, name="Odoo system")
    mine = await credential_row(app_session, agent.tenant_id, owner=anna)
    await _bind(app_session, agent, tool_key="odoo", mode="user")
    access = await _access(app_session, agent, conn, run)
    assert (access.kind, access.credential.id) == ("credential", mine)  # type: ignore[union-attr]


async def test_trigger_owner_removed_fails_cleanly(app_session: AppSessionFactory) -> None:
    agent, anna, conn, _run = await _setup(app_session)
    await oauth_row(app_session, agent.tenant_id, owner=anna)
    async with app_session(agent.tenant_id) as db:
        trigger = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            kind="cron",
            task_text="t",
            cron_expression="0 8 * * *",
            created_by_member_id=anna,
        )
        db.add(trigger)
        await db.flush()
        run = m.AgentRun(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            source="cron",
            context={"trigger_id": str(trigger.id)},
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        await purge_member_connections(db, tenant_id=agent.tenant_id, member_id=anna)
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run_id)
    assert exc.value.reason == "not_connected"


async def _revision(app_session: AppSessionFactory, agent: m.Agent) -> int:
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        return live.config_revision


async def _edit_agent(app_session: AppSessionFactory, agent: m.Agent) -> None:
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.mission = "changed by somebody else"


async def test_an_owners_own_binding_works_until_the_agent_is_edited(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _setup(app_session)
    annas = await oauth_row(app_session, agent.tenant_id, owner=anna)
    await _bind(
        app_session,
        agent,
        tool_key="microsoft365",
        mode="fixed",
        oauth_connection_id=annas,
        set_by_member_id=anna,
        agent_revision=await _revision(app_session, agent),
    )
    assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]
    await _edit_agent(app_session, agent)
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "binding_needs_confirmation"
    # Re-confirming (the owner saves the binding again) stores the new revision.
    async with app_session(agent.tenant_id) as db:
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent.id)
        )
        assert row is not None
        row.agent_revision = await _revision(app_session, agent)
    assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]


async def test_a_third_partys_binding_to_a_personal_account_is_refused(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _setup(app_session)
    ben = await member(app_session, agent.tenant_id, "ben@acme.test")
    annas = await oauth_row(app_session, agent.tenant_id, owner=anna)
    await _bind(
        app_session,
        agent,
        tool_key="microsoft365",
        mode="fixed",
        oauth_connection_id=annas,
        set_by_member_id=ben,
        set_with_manage=False,
        agent_revision=await _revision(app_session, agent),
    )
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "binding_needs_confirmation"


async def test_an_administrators_binding_to_a_personal_account_works(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _setup(app_session)
    root = await member(app_session, agent.tenant_id, "root@acme.test")
    annas = await oauth_row(app_session, agent.tenant_id, owner=anna)
    await _bind(
        app_session,
        agent,
        tool_key="microsoft365",
        mode="fixed",
        oauth_connection_id=annas,
        set_by_member_id=root,
        set_with_manage=True,
        agent_revision=await _revision(app_session, agent),
    )
    assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]


async def test_an_organisation_target_needs_no_consent(app_session: AppSessionFactory) -> None:
    agent, _anna, conn, run = await _setup(app_session)
    shared = await oauth_row(app_session, agent.tenant_id, owner=None, label="shared@acme.test")
    await _bind(
        app_session, agent, tool_key="microsoft365", mode="fixed", oauth_connection_id=shared
    )
    assert (await _access(app_session, agent, conn, run)).oauth_connection.id == shared  # type: ignore[union-attr]


async def test_a_fixed_target_that_needs_reconnecting_asks_to_reconnect(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _setup(app_session)
    shared = await oauth_row(app_session, agent.tenant_id, owner=None, status="needs_reauth")
    await _bind(
        app_session, agent, tool_key="microsoft365", mode="fixed", oauth_connection_id=shared
    )
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "needs_reauth"


async def test_an_access_setting_applies_to_a_pinned_login_too(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _setup(app_session, name="odoo", cfg=ODOO_CFG)
    pinned = await credential_row(app_session, agent.tenant_id, owner=None, name="Pinned")
    mine = await credential_row(app_session, agent.tenant_id, owner=anna)
    async with app_session(agent.tenant_id) as db:
        row = await db.get(m.McpConnection, conn)
        assert row is not None
        row.credential_id = pinned
    assert (await _access(app_session, agent, conn, run)).kind == "legacy"
    await _bind(app_session, agent, tool_key="odoo", mode="user")
    access = await _access(app_session, agent, conn, run)
    assert (access.kind, access.credential.id) == ("credential", mine)  # type: ignore[union-attr]
