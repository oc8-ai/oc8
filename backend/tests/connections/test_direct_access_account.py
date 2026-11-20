"""§4 account choice: the person's own login wins; otherwise the organisation
account, only with permission and only in a direct-access run. Every other
path keeps refusing a fallback."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.connections.access import (
    ConnectionRequired,
    account_of,
    resolve_direct_access,
    resolve_tool_access,
)
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row, enable_plugin_row, member, oauth_row

pytestmark = pytest.mark.asyncio

ODOO_CFG: dict[str, Any] = {
    "_plugin_name": "odoo_mcp",
    "_connection_key": "primary",
    "command": "uv",
    "args": ["tool", "run", "mcp-server-odoo"],
    "env": {},
    "secret_env": {},
}
MS_CFG: dict[str, Any] = {
    "_plugin_name": "microsoft365",
    "_connection_key": "primary",
    "command": "python",
    "args": ["-m", "mcp_bridge"],
    "env": {},
    "secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"},
}


async def _office(
    app_session: AppSessionFactory,
    *,
    seated: bool,
    name: str = "odoo",
    cfg: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """(tenant, member, connection, copilot)"""
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        copilot = await get_or_create_assistant(db, tenant_id=tenant)
        dept = m.Department(
            tenant_id=tenant,
            name="Sales",
            frame={"tools": {name: {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        if seated:
            db.add(
                m.OrgMemberDepartment(
                    tenant_id=tenant, member_id=anna, department_id=dept.id, seat_role="dept_viewer"
                )
            )
        conn = m.McpConnection(
            tenant_id=tenant,
            name=name,
            server_url="",
            transport="stdio",
            connected=True,
            config=dict(cfg or ODOO_CFG),
        )
        db.add(conn)
        await db.flush()
        conn_id, copilot_id = conn.id, copilot.id
    await enable_plugin_row(app_session, tenant, str((cfg or ODOO_CFG)["_plugin_name"]))
    return tenant, anna, conn_id, copilot_id


async def _direct(
    app_session: AppSessionFactory, tenant: uuid.UUID, anna: uuid.UUID, conn_id: uuid.UUID
) -> Any:
    async with app_session(tenant) as db:
        conn = await db.get(m.McpConnection, conn_id)
        assert conn is not None
        return await resolve_direct_access(db, member_id=anna, conn=conn)


async def test_the_persons_own_login_wins(app_session: AppSessionFactory) -> None:
    tenant, anna, conn, _ = await _office(app_session, seated=True)
    own = await credential_row(app_session, tenant, owner=anna)
    await credential_row(app_session, tenant, owner=None, name="Org")
    access = await _direct(app_session, tenant, anna, conn)
    assert access.credential.id == own
    assert account_of(access) == "own"


async def test_the_organisation_account_with_permission(app_session: AppSessionFactory) -> None:
    tenant, anna, conn, _ = await _office(app_session, seated=True)
    org = await credential_row(app_session, tenant, owner=None, name="Org")
    access = await _direct(app_session, tenant, anna, conn)
    assert access.credential.id == org and access.on_behalf_of == anna
    assert account_of(access) == "organisation"


async def test_no_organisation_account_without_permission(app_session: AppSessionFactory) -> None:
    tenant, anna, conn, _ = await _office(app_session, seated=False)
    await credential_row(app_session, tenant, owner=None, name="Org")
    with pytest.raises(ConnectionRequired) as exc:
        await _direct(app_session, tenant, anna, conn)
    assert exc.value.reason == "not_connected"


async def test_resolve_tool_access_uses_the_direct_branch_only_with_a_grant(
    app_session: AppSessionFactory,
) -> None:
    tenant, anna, conn_id, copilot_id = await _office(app_session, seated=True)
    org = await credential_row(app_session, tenant, owner=None, name="Org")
    async with app_session(tenant) as db:
        session = m.ChatSession(tenant_id=tenant, agent_id=copilot_id, member_id=anna)
        db.add(session)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=copilot_id,
            action_type="direct_access",
            title="t",
            status="approved",
            payload={},
        )
        db.add(approval)
        await db.flush()
        db.add(
            m.DirectAccessGrant(
                tenant_id=tenant,
                chat_session_id=session.id,
                member_id=anna,
                tool_key="odoo",
                approval_id=approval.id,
            )
        )
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=copilot_id,
            source="chat",
            context={"chat_session_id": str(session.id), "door": "web", "on_behalf_of": str(anna)},
        )
        db.add(run)
        await db.flush()
        conn = await db.get(m.McpConnection, conn_id)
        copilot = await db.get(m.Agent, copilot_id)
        assert conn is not None and copilot is not None
        access = await resolve_tool_access(db, run=run, agent=copilot, conn=conn)
        assert access.credential is not None and access.credential.id == org


async def test_a_grant_of_another_member_never_lends_its_account(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, anna, conn_id, copilot_id = await _office(app_session, seated=True)
    bob = await member(app_session, tenant, "bob@acme.test")
    await credential_row(app_session, tenant, owner=None, name="Org")
    async with app_session(tenant) as db:
        session = m.ChatSession(tenant_id=tenant, agent_id=copilot_id, member_id=anna)
        db.add(session)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=copilot_id,
            action_type="direct_access",
            title="t",
            status="approved",
            payload={},
        )
        db.add(approval)
        await db.flush()
        # The conversation is Anna's, the grant row names Bob: fail closed.
        db.add(
            m.DirectAccessGrant(
                tenant_id=tenant,
                chat_session_id=session.id,
                member_id=bob,
                tool_key="odoo",
                approval_id=approval.id,
            )
        )
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=copilot_id,
            source="chat",
            context={"chat_session_id": str(session.id), "door": "web", "on_behalf_of": str(anna)},
        )
        db.add(run)
        await db.flush()
        conn = await db.get(m.McpConnection, conn_id)
        copilot = await db.get(m.Agent, copilot_id)
        assert conn is not None and copilot is not None
        taken: list[object] = []

        async def _spy(*_a: Any, **_kw: Any) -> Any:
            taken.append(1)
            raise AssertionError("the direct branch must not run")

        monkeypatch.setattr("oc8.connections.access.resolve_direct_access", _spy)
        await resolve_tool_access(db, run=run, agent=copilot, conn=conn)
        assert taken == []


async def test_other_paths_still_refuse_a_fallback(app_session: AppSessionFactory) -> None:
    """Regression: a user-mode integration on an ordinary agent's chat run --
    even one carrying on_behalf_of -- never borrows the organisation's account."""
    tenant, anna, conn_id, _copilot = await _office(
        app_session, seated=True, name="microsoft365", cfg=MS_CFG
    )
    await oauth_row(app_session, tenant, owner=None, label="shared@acme.test")
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="chat",
            context={"door": "web", "on_behalf_of": str(anna)},
        )
        db.add(run)
        await db.flush()
        conn = await db.get(m.McpConnection, conn_id)
        assert conn is not None
        with pytest.raises(ConnectionRequired) as exc:
            await resolve_tool_access(db, run=run, agent=agent, conn=conn)
        assert exc.value.reason == "not_connected"
