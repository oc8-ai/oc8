"""Which account a tool call acts through. The rules that matter most: user
mode uses the acting person's own connection or nothing; a fixed binding uses
its target or nothing; a department's app-only setup keeps working."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.connections.access import (
    ConnectionRequired,
    ToolAccess,
    binding_fingerprint,
    resolve_tool_access,
    trigger_fingerprint,
)
from oc8.connections.revoke import purge_member_connections
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row, enable_plugin_row, member, oauth_row
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
    await enable_plugin_row(app_session, agent.tenant_id, str(dict(cfg or MS_CFG)["_plugin_name"]))
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
        binding_fingerprint=await _fp(app_session, agent),
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
        trigger.access_fingerprint = await trigger_fingerprint(db, trigger)  # Anna saved it
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


async def _fp(app_session: AppSessionFactory, agent: m.Agent, tool: str = "microsoft365") -> str:
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        return await binding_fingerprint(db, live, tool, version_id=live.current_version_id)


async def _edit_agent(app_session: AppSessionFactory, agent: m.Agent) -> None:
    """Publishing a new agent version."""
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.current_version_id = uuid.uuid4()


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
        binding_fingerprint=await _fp(app_session, agent),
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
        row.binding_fingerprint = await _fp(app_session, agent)
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
        binding_fingerprint=await _fp(app_session, agent),
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
        binding_fingerprint=await _fp(app_session, agent),
    )
    assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]


async def test_an_organisation_target_set_by_an_administrator_works(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _setup(app_session)
    shared = await oauth_row(app_session, agent.tenant_id, owner=None, label="shared@acme.test")
    await _bind(
        app_session,
        agent,
        tool_key="microsoft365",
        mode="fixed",
        oauth_connection_id=shared,
        set_with_manage=True,
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


async def _bound_to_anna(
    app_session: AppSessionFactory, **kw: Any
) -> tuple[m.Agent, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    agent, anna, conn, run = await _setup(app_session, **kw)
    annas = await oauth_row(app_session, agent.tenant_id, owner=anna)
    await _bind(
        app_session,
        agent,
        tool_key="microsoft365",
        mode="fixed",
        oauth_connection_id=annas,
        set_by_member_id=anna,
        binding_fingerprint=await _fp(app_session, agent),
    )
    return agent, anna, conn, run, annas


async def test_ordinary_agent_activity_keeps_a_binding_valid(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run, annas = await _bound_to_anna(app_session)
    for status in ("running", "idle"):
        async with app_session(agent.tenant_id) as db:
            live = await db.get(m.Agent, agent.id)
            assert live is not None
            live.status = status
            live.definition = {
                **live.definition,
                "auto_router_affinity": {"tier": "x", "config_id": "y"},
            }
        assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]


async def test_a_skill_or_department_change_needs_confirmation(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run, _annas = await _bound_to_anna(app_session)
    async with app_session(agent.tenant_id) as db:
        skill = m.Skill(tenant_id=agent.tenant_id, name="s")
        db.add(skill)
        await db.flush()
        version = m.SkillVersion(
            tenant_id=agent.tenant_id,
            skill_id=skill.id,
            semver="1.0.0",
            definition={},
            artifact_hash=b"x",
        )
        db.add(version)
        await db.flush()
        db.add(
            m.SkillAssignment(
                tenant_id=agent.tenant_id, agent_id=agent.id, skill_version_id=version.id
            )
        )
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "binding_needs_confirmation"
    # Confirm again, then change the department's frame.
    async with app_session(agent.tenant_id) as db:
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent.id)
        )
        assert row is not None
        row.binding_fingerprint = await _fp(app_session, agent)
    await _access(app_session, agent, conn, run)
    async with app_session(agent.tenant_id) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {**dept.frame, "allowed": ["something new"]}
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "binding_needs_confirmation"


async def test_a_trigger_acts_through_a_personal_binding_only_for_its_owner(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, _run, annas = await _bound_to_anna(app_session)
    ben = await member(app_session, agent.tenant_id, "ben@acme.test")

    async def fire(creator: uuid.UUID) -> uuid.UUID:
        async with app_session(agent.tenant_id) as db:
            trigger = m.Trigger(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                kind="cron",
                task_text="t",
                cron_expression="0 8 * * *",
                created_by_member_id=creator,
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
            return run.id

    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, await fire(ben))
    assert exc.value.reason == "binding_needs_confirmation"
    assert (await _access(app_session, agent, conn, await fire(anna))).oauth_connection.id == annas  # type: ignore[union-attr]


async def test_an_organisation_target_needs_manage_or_the_departments_own_setup(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _setup(app_session)
    shared = await oauth_row(app_session, agent.tenant_id, owner=None, label="shared@acme.test")
    await _bind(
        app_session, agent, tool_key="microsoft365", mode="fixed", oauth_connection_id=shared
    )
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "binding_needs_confirmation"
    async with app_session(agent.tenant_id) as db:
        row = await db.get(m.McpConnection, conn)
        assert row is not None
        row.department_id = agent.department_id
        row.config = {**row.config, "oauth_connection_id": str(shared)}
    assert (await _access(app_session, agent, conn, run)).oauth_connection.id == shared  # type: ignore[union-attr]


async def _confirm_again(app_session: AppSessionFactory, agent: m.Agent) -> None:
    async with app_session(agent.tenant_id) as db:
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent.id)
        )
        assert row is not None
        row.binding_fingerprint = await _fp(app_session, agent)


@pytest.mark.parametrize(
    "change",
    [
        {"config": {**MS_CFG, "secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:other_mint"}}},
        {"config": {**MS_CFG, "env": {**MS_CFG["env"], "MAILBOX_KEY": "boss@acme.test"}}},
        {"server_url": "https://elsewhere.test/mcp"},
        {"transport": "http"},
        {"scopes": {"send_mail": "read"}},
    ],
)
async def test_any_change_to_the_departments_tool_setup_needs_confirmation(
    app_session: AppSessionFactory, change: dict[str, Any]
) -> None:
    agent, _anna, conn, run, _annas = await _bound_to_anna(app_session, department=True)
    await _access(app_session, agent, conn, run)
    async with app_session(agent.tenant_id) as db:
        row = await db.get(m.McpConnection, conn)
        assert row is not None
        for attr, value in change.items():
            setattr(row, attr, value)
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "binding_needs_confirmation"


async def test_an_agent_without_a_version_is_fingerprinted_by_its_working_copy(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.current_version_id = None
    agent, _anna, conn, run, annas = await _bound_to_anna_on(app_session, agent)
    assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.mission = "Something else entirely"
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "binding_needs_confirmation"


async def _bound_to_anna_on(
    app_session: AppSessionFactory, agent: m.Agent
) -> tuple[m.Agent, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    async with app_session(agent.tenant_id) as db:
        conn = m.McpConnection(
            tenant_id=agent.tenant_id,
            name="microsoft365",
            server_url="",
            transport="stdio",
            config=dict(MS_CFG),
        )
        db.add(conn)
        run = m.AgentRun(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            source="manual",
            context={"originating_operator": "anna@acme.test"},
        )
        db.add(run)
        await db.flush()
        conn_id, run_id = conn.id, run.id
    await enable_plugin_row(app_session, agent.tenant_id, "microsoft365")
    annas = await oauth_row(app_session, agent.tenant_id, owner=anna)
    await _bind(
        app_session,
        agent,
        tool_key="microsoft365",
        mode="fixed",
        oauth_connection_id=annas,
        set_by_member_id=anna,
        binding_fingerprint=await _fp(app_session, agent),
    )
    return agent, anna, conn_id, run_id, annas


async def _trigger(
    app_session: AppSessionFactory, agent: m.Agent, creator: uuid.UUID, **fields: Any
) -> uuid.UUID:
    async with app_session(agent.tenant_id) as db:
        trigger = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            task_text="t",
            created_by_member_id=creator,
            **fields,
        )
        db.add(trigger)
        await db.flush()
        return trigger.id


async def _run(
    app_session: AppSessionFactory, agent: m.Agent, context: dict[str, Any]
) -> uuid.UUID:
    async with app_session(agent.tenant_id) as db:
        run = m.AgentRun(
            tenant_id=agent.tenant_id, agent_id=agent.id, source="chat", context=context
        )
        db.add(run)
        await db.flush()
        return run.id


async def test_a_copilot_follow_up_counts_as_its_triggers_run(
    app_session: AppSessionFactory,
) -> None:
    import datetime as dt

    agent, anna, conn, _run_id, annas = await _bound_to_anna(app_session)
    ben = await member(app_session, agent.tenant_id, "ben@acme.test")
    due = dt.datetime.now(dt.UTC)
    for creator, allowed in ((ben, False), (anna, True)):
        trigger = await _trigger(app_session, agent, creator, kind="once", next_run_at=due)
        run = await _run(
            app_session,
            agent,
            {
                "originating_operator": "anna@acme.test",
                "followup": {"responsibility_id": str(uuid.uuid4()), "trigger_id": str(trigger)},
            },
        )
        if allowed:
            assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]
        else:
            with pytest.raises(ConnectionRequired) as exc:
                await _access(app_session, agent, conn, run)
            assert exc.value.reason == "binding_needs_confirmation"


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "webhook", "webhook_token": None},
        {"kind": "event", "event_source": "github", "event_type": "push"},
    ],
)
async def test_a_webhook_or_event_trigger_needs_acts_for_creator(
    app_session: AppSessionFactory, fields: dict[str, Any]
) -> None:
    agent, anna, conn, _run_id, annas = await _bound_to_anna(app_session)
    for acts in (False, True):
        extra = dict(fields)
        if "webhook_token" in extra:
            extra["webhook_token"] = uuid.uuid4().hex
        trigger = await _trigger(app_session, agent, anna, acts_for_creator=acts, **extra)
        run = await _run(app_session, agent, {"trigger_id": str(trigger)})
        if acts:
            assert (await _access(app_session, agent, conn, run)).oauth_connection.id == annas  # type: ignore[union-attr]
        else:
            with pytest.raises(ConnectionRequired) as exc:
                await _access(app_session, agent, conn, run)
            assert exc.value.reason == "binding_needs_confirmation"
