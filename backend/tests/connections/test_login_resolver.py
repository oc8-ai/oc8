"""One login resolver for agents and the Copilot (unified connections design,
"Resolver order (agents)" and "Resolver order (Copilot direct access)").

Built on a throwaway tool pack and login type, so no vendor is involved."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from oc8 import config
from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.capas.discovery import invalidate_discovery_cache
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.connections import runtime_env
from oc8.connections.access import (
    ConnectionRequired,
    ToolAccess,
    resolve_direct_access,
    resolve_tool_access,
)
from oc8.connections.integrations import integration_for_connection
from oc8.connections.login import resolve_login
from oc8.connections.read_only import ReadOnlyBridge
from oc8.connections.runtime_env import run_mcp_env
from oc8.credentials import registry
from oc8.credentials.enablement import set_departments
from oc8.credentials.service import update_credential
from tests.conftest import AppSessionFactory
from tests.connections.helpers import enable_plugin_row, member
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio

PLUGIN = "test_login_pack"
KEY = "test_login_tool"
TYPE = "test_login"

PLUGIN_TOML = f"""
[plugin]
name = "{PLUGIN}"
label = "Test Login"
version = "1.0.0"
type = "tool_pack"
trust = "first_party"
summary = "A throwaway tool pack whose bridge takes a login"
permissions = ["mcp:connect", "secrets:use"]
"""

TOOL_PACK_TOML = f"""
[[connections]]
key = "primary"
name = "{KEY}"
transport = "stdio"
server_url = ""
credential_type = "{TYPE}"

[connections.scopes]
read = ["look"]
modify = ["change"]

[connections.credential_env_fields]
LOGIN_URL = "url"
LOGIN_TOKEN = "token"

[connections.config]
command = "python"
args = ["-m", "bridge"]

[connections.config.env]
LOGIN_MODE = "plain"
"""

CFG: dict[str, Any] = {
    "_plugin_name": PLUGIN,
    "_connection_key": "primary",
    "command": "python",
    "args": ["-m", "bridge"],
    "env": {"LOGIN_MODE": "plain"},
    "secret_env": {},
}

FRAME = {"tools": {KEY: {"enabled": True, "read": True, "modify": True}}}


@pytest.fixture(autouse=True)
def _test_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _connection_settings: None
) -> Iterator[None]:
    pack = tmp_path / "capas" / PLUGIN
    pack.mkdir(parents=True)
    (pack / "plugin.toml").write_text(PLUGIN_TOML)
    (pack / "tool_pack.toml").write_text(TOOL_PACK_TOML)
    real = config.get_settings().capas_path
    monkeypatch.setenv("OC8_CAPAS_PATH", f"{tmp_path / 'capas'},{real}")
    config.get_settings.cache_clear()
    invalidate_discovery_cache()
    registry.CORE_CREDENTIAL_TYPES[TYPE] = CredentialTypeSpec(
        name=TYPE,
        display_name="Test login",
        fields=[
            SetupFieldSpec(key="url", label="URL", kind="url"),
            SetupFieldSpec(key="token", label="Token", kind="password", required=False),
        ],
    )
    try:
        yield
    finally:
        registry.CORE_CREDENTIAL_TYPES.pop(TYPE, None)
        invalidate_discovery_cache()


# ------------------------------------------------------------------ fixtures


async def _world(
    app_session: AppSessionFactory,
    *,
    context: dict[str, Any] | None = None,
    source: str = "manual",
    department_row: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[m.Agent, uuid.UUID, uuid.UUID, uuid.UUID]:
    """(agent, anna, connection, run). Anna is seated in the agent's department."""
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    async with app_session(agent.tenant_id) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = FRAME
        conn = m.McpConnection(
            tenant_id=agent.tenant_id,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            config={**CFG, "env": {**CFG["env"], **(env or {})}},
            department_id=agent.department_id if department_row else None,
        )
        db.add(conn)
        session = m.ChatSession(tenant_id=agent.tenant_id, agent_id=agent.id, member_id=anna)
        db.add(session)
        await db.flush()
        ctx = context if context is not None else {"originating_operator": "anna@acme.test"}
        ctx = {k: (str(session.id) if v == "<session>" else v) for k, v in ctx.items()}
        run = m.AgentRun(tenant_id=agent.tenant_id, agent_id=agent.id, source=source, context=ctx)
        db.add(run)
        await db.flush()
        conn_id, run_id = conn.id, run.id
    await enable_plugin_row(app_session, agent.tenant_id, PLUGIN)
    return agent, anna, conn_id, run_id


LIVE_CHAT = {
    "originating_operator": "anna@acme.test",
    "chat_session_id": "<session>",
    "door": "web",
}


async def _login(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    name: str,
    owner: uuid.UUID | None = None,
    url: str = "https://svc.acme.test",
    departments: set[uuid.UUID] | None = None,
    all_departments: bool = False,
) -> uuid.UUID:
    async with app_session(tenant) as db:
        row = m.Credential(
            tenant_id=tenant,
            name=name,
            credential_type=TYPE,
            field_values={"url": url} if url else {},
            secret_refs={},
            owner_member_id=owner,
        )
        db.add(row)
        await db.flush()
        if departments or all_departments:
            await set_departments(
                db, row, department_ids=departments or set(), all_departments=all_departments
            )
        return row.id


async def _access(
    app_session: AppSessionFactory, agent: m.Agent, conn_id: uuid.UUID, run_id: uuid.UUID
) -> ToolAccess:
    async with app_session(agent.tenant_id) as db:
        conn = await db.get(m.McpConnection, conn_id)
        run = await db.get(m.AgentRun, run_id)
        live = await db.get(m.Agent, agent.id)
        assert conn is not None and run is not None and live is not None
        return await resolve_tool_access(db, run=run, agent=live, conn=conn)


async def _department(
    app_session: AppSessionFactory, tenant: uuid.UUID, name: str, frame: dict[str, Any]
) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name=name, frame=frame)
        db.add(dept)
        await db.flush()
        return dept.id


# ------------------------------------------------------------------ agents


async def test_fixed_choice_wins_over_department_enablement(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(app_session)
    await _login(app_session, agent.tenant_id, name="Enabled", departments={agent.department_id})
    chosen = await _login(app_session, agent.tenant_id, name="Chosen")
    async with app_session(agent.tenant_id) as db:
        db.add(
            m.AgentToolAccess(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                tool_key=KEY,
                mode="fixed",
                credential_id=chosen,
                set_with_manage=True,
            )
        )
    access = await _access(app_session, agent, conn, run)
    assert (access.kind, access.credential.id) == ("credential", chosen)  # type: ignore[union-attr]


async def test_live_chat_uses_the_persons_own_login(app_session: AppSessionFactory) -> None:
    agent, anna, conn, run = await _world(app_session, context=LIVE_CHAT, source="chat")
    await _login(app_session, agent.tenant_id, name="Dept", departments={agent.department_id})
    own = await _login(app_session, agent.tenant_id, name="Mine", owner=anna)
    access = await _access(app_session, agent, conn, run)
    assert (access.credential.id, access.on_behalf_of) == (own, anna)  # type: ignore[union-attr]


async def test_a_scheduled_run_ignores_the_persons_own_login(
    app_session: AppSessionFactory,
) -> None:
    agent, anna, conn, run = await _world(app_session, source="manual")
    dept = await _login(
        app_session, agent.tenant_id, name="Dept", departments={agent.department_id}
    )
    await _login(app_session, agent.tenant_id, name="Mine", owner=anna)
    access = await _access(app_session, agent, conn, run)
    assert access.credential.id == dept  # type: ignore[union-attr]


async def test_the_login_enabled_for_the_agents_department_is_used(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(app_session)
    other = await _department(app_session, agent.tenant_id, "Other", FRAME)
    await _login(app_session, agent.tenant_id, name="Other's", departments={other})
    mine = await _login(
        app_session, agent.tenant_id, name="Ours", departments={agent.department_id}
    )
    access = await _access(app_session, agent, conn, run)
    assert (access.kind, access.credential.id) == ("credential", mine)  # type: ignore[union-attr]
    async with app_session(agent.tenant_id) as db:
        conn_row = await db.get(m.McpConnection, conn)
        live = await db.get(m.Agent, agent.id)
        assert conn_row is not None and live is not None
        integration = integration_for_connection(conn_row)
        assert integration is not None
        choice = await resolve_login(
            db,
            agent=live,
            integration=integration,
            conn=conn_row,
            live_member_id=None,
            department_ids=[agent.department_id],
            right="read",
        )
    assert (choice.source, choice.credential.id) == ("department", mine)  # type: ignore[union-attr]


async def test_two_enabled_logins_without_a_choice_are_ambiguous(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(app_session)
    await _login(app_session, agent.tenant_id, name="One", departments={agent.department_id})
    await _login(app_session, agent.tenant_id, name="Two", departments={agent.department_id})
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "ambiguous"
    assert str(exc.value) == (
        "connection_required: Several Test Login logins are enabled for this department "
        "and the agent has none chosen."
    )


async def test_a_direct_department_choice_wins_over_an_all_departments_login(
    app_session: AppSessionFactory,
) -> None:
    """A login chosen for the department itself beats one enabled for all
    departments; only two of the same kind are ambiguous."""
    agent, _anna, conn, run = await _world(app_session)
    await _login(app_session, agent.tenant_id, name="Everyone", all_departments=True)
    ours = await _login(
        app_session, agent.tenant_id, name="Ours", departments={agent.department_id}
    )
    access = await _access(app_session, agent, conn, run)
    assert access.credential.id == ours  # type: ignore[union-attr]
    await _login(app_session, agent.tenant_id, name="Everyone too", all_departments=True)
    access = await _access(app_session, agent, conn, run)
    assert access.credential.id == ours  # type: ignore[union-attr]


async def test_two_all_departments_logins_are_ambiguous(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(app_session)
    await _login(app_session, agent.tenant_id, name="One", all_departments=True)
    await _login(app_session, agent.tenant_id, name="Two", all_departments=True)
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "ambiguous"


async def test_single_organisation_login_is_used_when_nothing_is_enabled(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(app_session)
    only = await _login(app_session, agent.tenant_id, name="Only")
    access = await _access(app_session, agent, conn, run)
    assert access.credential.id == only  # type: ignore[union-attr]


async def test_an_enabled_login_of_another_department_is_not_used(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(app_session)
    other = await _department(app_session, agent.tenant_id, "Other", FRAME)
    # The tenant's only login, but enabled for another department: step 4
    # (single organisation login) no longer applies once enablement is used.
    await _login(app_session, agent.tenant_id, name="Other's", departments={other})
    with pytest.raises(ConnectionRequired) as exc:
        await _access(app_session, agent, conn, run)
    assert exc.value.reason == "not_configured"


async def test_unconsolidated_department_row_still_serves_its_agents(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(
        app_session, department_row=True, env={"LOGIN_URL": "https://copied.acme.test"}
    )
    other = await _department(app_session, agent.tenant_id, "Other", FRAME)
    await _login(app_session, agent.tenant_id, name="Other's", departments={other})
    access = await _access(app_session, agent, conn, run)
    assert access.kind == "legacy"
    async with app_session(agent.tenant_id) as db:
        conn_row = await db.get(m.McpConnection, conn)
        live = await db.get(m.Agent, agent.id)
        assert conn_row is not None and live is not None
        integration = integration_for_connection(conn_row)
        assert integration is not None
        choice = await resolve_login(
            db,
            agent=live,
            integration=integration,
            conn=conn_row,
            live_member_id=None,
            department_ids=[agent.department_id],
            right="read",
        )
    assert (choice.source, choice.legacy_conn.id) == ("legacy", conn)  # type: ignore[union-attr]


# ------------------------------------------------------------------ launch


async def test_an_incomplete_login_is_reported_not_started(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Any] = []

    async def _never(*args: Any, **kwargs: Any) -> dict[str, str]:
        started.append(kwargs)
        return {}

    monkeypatch.setattr(runtime_env, "resolve_mcp_env", _never)
    agent, _anna, conn, run = await _world(app_session)
    await _login(
        app_session, agent.tenant_id, name="Empty", url="", departments={agent.department_id}
    )
    ctx: dict[str, Any] = {}
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        assert run_row is not None and conn_row is not None
        with pytest.raises(ConnectionRequired) as call:
            await run_mcp_env(db, run=run_row, conn=conn_row, tool="look", ctx=ctx)
        with pytest.raises(ConnectionRequired) as listing:
            await run_mcp_env(db, run=run_row, conn=conn_row, listing=True)
    assert (call.value.reason, call.value.missing) == ("incomplete", ["url"])
    assert listing.value.reason == "incomplete"
    assert ctx["connection_required"]["missing"] == ["url"]
    assert str(call.value) == (
        "connection_required: The Test Login login is incomplete: url missing. "
        "Ask an admin to complete it under Settings → Connections."
    )
    assert started == []  # no bridge, not even a listing one without its login


async def test_editing_a_login_reaches_the_next_bridge_start(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, conn, run = await _world(app_session)
    login = await _login(
        app_session,
        agent.tenant_id,
        name="Ours",
        url="https://before.acme.test",
        departments={agent.department_id},
    )

    async def _launch() -> dict[str, str]:
        async with app_session(agent.tenant_id) as db:
            run_row = await db.get(m.AgentRun, run)
            conn_row = await db.get(m.McpConnection, conn)
            assert run_row is not None and conn_row is not None
            return (await run_mcp_env(db, run=run_row, conn=conn_row, tool="look")).env

    assert (await _launch())["LOGIN_URL"] == "https://before.acme.test"
    async with app_session(agent.tenant_id) as db:
        await update_credential(
            db,
            tenant_id=agent.tenant_id,
            credential_id=login,
            field_values={"url": "https://after.acme.test"},
        )
    assert (await _launch())["LOGIN_URL"] == "https://after.acme.test"


# ------------------------------------------------------------------ Copilot


async def test_copilot_write_needs_a_department_granting_modify(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    reading = await _department(
        app_session,
        tenant,
        "Reading",
        {"tools": {KEY: {"enabled": True, "read": True, "modify": False}}},
    )
    async with app_session(tenant) as db:
        await get_or_create_assistant(db, tenant_id=tenant)
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant, member_id=anna, department_id=reading, seat_role="dept_viewer"
            )
        )
        conn = m.McpConnection(
            tenant_id=tenant,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            config=dict(CFG),
        )
        db.add(conn)
        await db.flush()
        conn_id = conn.id
    await enable_plugin_row(app_session, tenant, PLUGIN)
    read_login = await _login(app_session, tenant, name="Read", departments={reading})

    async def _direct(right: str) -> ToolAccess:
        async with app_session(tenant) as db:
            row = await db.get(m.McpConnection, conn_id)
            assert row is not None
            return await resolve_direct_access(
                db,
                member_id=anna,
                conn=row,
                right=right,  # type: ignore[arg-type]
            )

    assert (await _direct("read")).credential.id == read_login  # type: ignore[union-attr]
    with pytest.raises(ConnectionRequired):
        # The reading department's login does not lend itself to a write.
        await _direct("modify")
    writing = await _department(app_session, tenant, "Writing", FRAME)
    async with app_session(tenant) as db:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant, member_id=anna, department_id=writing, seat_role="dept_viewer"
            )
        )
    write_login = await _login(app_session, tenant, name="Write", departments={writing})
    access = await _direct("modify")
    assert (access.credential.id, access.on_behalf_of) == (write_login, anna)  # type: ignore[union-attr]


async def _copilot_run(
    app_session: AppSessionFactory, *, seats: dict[str, dict[str, Any]]
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID], uuid.UUID, uuid.UUID]:
    """A live Copilot web turn of Anna's holding a direct-access grant for the
    tool. `seats`: department name -> frame; Anna sits in each. Returns
    (tenant, anna, department ids by name, connection, run)."""
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    departments = {
        name: await _department(app_session, tenant, name, frame) for name, frame in seats.items()
    }
    async with app_session(tenant) as db:
        copilot = await get_or_create_assistant(db, tenant_id=tenant)
        for dept in departments.values():
            db.add(
                m.OrgMemberDepartment(
                    tenant_id=tenant, member_id=anna, department_id=dept, seat_role="dept_viewer"
                )
            )
        conn = m.McpConnection(
            tenant_id=tenant,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            config=dict(CFG),
        )
        session = m.ChatSession(tenant_id=tenant, agent_id=copilot.id, member_id=anna)
        db.add_all([conn, session])
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=copilot.id,
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
                tool_key=KEY,
                approval_id=approval.id,
            )
        )
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=copilot.id,
            source="chat",
            context={"chat_session_id": str(session.id), "door": "web", "on_behalf_of": str(anna)},
        )
        db.add(run)
        await db.flush()
        conn_id, run_id = conn.id, run.id
    await enable_plugin_row(app_session, tenant, PLUGIN)
    return tenant, anna, departments, conn_id, run_id


READ_ONLY = {"tools": {KEY: {"enabled": True, "read": True, "modify": False}}}


class _FakeSession:
    def __init__(self) -> None:
        self.tools = [type("T", (), {"name": n})() for n in ("look", "change")]
        self.calls: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        return "ok"


async def test_copilot_cannot_write_through_a_login_resolved_for_reading(
    app_session: AppSessionFactory,
) -> None:
    tenant, _anna, depts, conn, run = await _copilot_run(app_session, seats={"Reading": READ_ONLY})
    read_login = await _login(app_session, tenant, name="Read", departments={depts["Reading"]})
    async with app_session(tenant) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        assert run_row is not None and conn_row is not None
        # The whole-run bridge falls back to the reading department's login,
        # and says it was resolved for reading only.
        bridge = await run_mcp_env(db, run=run_row, conn=conn_row, tool="*")
        assert (bridge.env["LOGIN_URL"], bridge.right) == ("https://svc.acme.test", "read")
        # A single write call is resolved for modify: nothing lends it a login.
        with pytest.raises(ConnectionRequired):
            await run_mcp_env(db, run=run_row, conn=conn_row, tool="change")
        assert (await run_mcp_env(db, run=run_row, conn=conn_row, tool="look")).right == "read"
        inner = _FakeSession()
        async with ReadOnlyBridge(inner, conn_row) as session:
            assert [t.name for t in session.tools] == ["look"]
            assert await session.call("look", {}) == "ok"
            with pytest.raises(ConnectionRequired) as refused:
                await session.call("change", {})
    assert refused.value.reason == "read_only"
    assert inner.calls == ["look"]  # the write never reached the bridge
    assert read_login


async def test_a_whole_run_bridge_resolves_with_modify_when_the_tool_can_write(
    app_session: AppSessionFactory,
) -> None:
    tenant, _anna, depts, conn, run = await _copilot_run(
        app_session, seats={"Reading": READ_ONLY, "Writing": FRAME}
    )
    await _login(
        app_session,
        tenant,
        name="Read",
        url="https://read.acme.test",
        departments={depts["Reading"]},
    )
    await _login(
        app_session,
        tenant,
        name="Write",
        url="https://write.acme.test",
        departments={depts["Writing"]},
    )
    async with app_session(tenant) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        assert run_row is not None and conn_row is not None
        bridge = await run_mcp_env(db, run=run_row, conn=conn_row, tool="*")
    assert (bridge.env["LOGIN_URL"], bridge.right) == ("https://write.acme.test", "modify")


async def test_listing_without_a_login_does_not_ask_for_a_connection(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Any] = []

    async def _never(*args: Any, **kwargs: Any) -> dict[str, str]:
        started.append(kwargs)
        return {}

    monkeypatch.setattr(runtime_env, "resolve_mcp_env", _never)
    agent, _anna, conn, run = await _world(app_session)  # no login at all
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        assert run_row is not None and conn_row is not None
        with pytest.raises(ConnectionRequired) as listing:
            await run_mcp_env(db, run=run_row, conn=conn_row, listing=True)
    assert listing.value.reason == "not_configured"
    assert started == []  # no bridge
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        assert run_row is not None
        assert "connection_required" not in (run_row.context or {})
        conn_row = await db.get(m.McpConnection, conn)
        assert conn_row is not None
        # Only an actual call asks for the connection.
        with pytest.raises(ConnectionRequired):
            await run_mcp_env(db, run=run_row, conn=conn_row, tool="look")
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        assert run_row is not None
        assert run_row.context["connection_required"]["reason"] == "not_configured"


INCOMPLETE_LINE = (
    "connection_required: The Test Login login is incomplete: url missing. "
    "Ask an admin to complete it under Settings → Connections."
)


async def test_listing_records_a_notice_not_a_connection_prompt(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete (or ambiguous) login is an admin's configuration error:
    a listing records a quiet notice on the run naming the tool -- never the
    connect prompt a call raises."""

    async def _never(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise AssertionError("no bridge for an incomplete login")

    monkeypatch.setattr(runtime_env, "resolve_mcp_env", _never)
    agent, _anna, conn, run = await _world(app_session)
    await _login(
        app_session, agent.tenant_id, name="Empty", url="", departments={agent.department_id}
    )
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        assert run_row is not None and conn_row is not None
        for _ in range(2):  # a second listing does not repeat it
            with pytest.raises(ConnectionRequired):
                await run_mcp_env(db, run=run_row, conn=conn_row, listing=True)
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        assert run_row is not None
        assert "connection_required" not in run_row.context
        assert run_row.context["tool_notices"] == [
            {
                "tool": KEY,
                "integration": "test_login_pack",
                "label": "Test Login",
                "reason": "incomplete",
                "missing": ["url"],
            }
        ]


async def test_listing_without_any_login_records_no_notice(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _anna, conn, run = await _world(app_session)  # no login at all
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        assert run_row is not None and conn_row is not None
        with pytest.raises(ConnectionRequired):
            await run_mcp_env(db, run=run_row, conn=conn_row, listing=True)
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        assert run_row is not None
        assert "tool_notices" not in (run_row.context or {})


async def test_an_incomplete_login_is_named_to_the_model_in_isolated_runtimes(
    app_session: AppSessionFactory,
) -> None:
    """Both isolated runtimes tell the model, in one line, why the tool is
    missing: the step endpoint's listing note and the gateway's
    `initialize` instructions -- the same words a call would raise."""
    from oc8.agent.offering import connection_notes
    from oc8.api.mcp_gateway import _initialize_result
    from oc8.api.v1.internal_agent import _discover_connection_tools

    agent, _anna, conn, run = await _world(app_session)
    await _login(
        app_session, agent.tenant_id, name="Empty", url="", departments={agent.department_id}
    )
    async with app_session(agent.tenant_id) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = FRAME
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        live = await db.get(m.Agent, agent.id)
        dept = await db.get(m.Department, agent.department_id)
        assert run_row is not None and conn_row is not None and live is not None
        schemas, _routes, unavailable, _res, _prompts = await _discover_connection_tools(
            db, run=run_row, agent=live, dept=dept, conns=[conn_row], pinned={"narrowing": {}}
        )
        assert schemas == []
        assert connection_notes(unavailable) == [INCOMPLETE_LINE]
        result = await _initialize_result(
            db, run=run_row, agent=live, dept=dept, conns=[conn_row]
        )
    assert result["instructions"] == INCOMPLETE_LINE


async def test_an_ambiguous_login_joins_the_run_and_is_named(
    app_session: AppSessionFactory,
) -> None:
    """Several logins and none chosen: the tool is not dropped before its
    listing, which then names the problem."""
    from oc8.api.mcp_gateway import _initialize_result
    from oc8.authz.pdp import effective_tool_policies
    from oc8.connections.runtime_env import integration_connections

    agent, _anna, conn, run = await _world(app_session)
    for name in ("One", "Two"):
        await _login(app_session, agent.tenant_id, name=name, departments={agent.department_id})
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        live = await db.get(m.Agent, agent.id)
        assert run_row is not None and live is not None
        joined = await integration_connections(
            db, agent=live, policies=effective_tool_policies(FRAME, {}), have=set(), run=run_row
        )
        assert [c.id for c in joined] == [conn]
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = FRAME
        result = await _initialize_result(db, run=run_row, agent=live, dept=dept, conns=joined)
    assert result["instructions"] == (
        "connection_required: Several Test Login logins are enabled for this department "
        "and the agent has none chosen."
    )
    async with app_session(agent.tenant_id) as db:
        run_row = await db.get(m.AgentRun, run)
        assert run_row is not None
        assert [n["reason"] for n in run_row.context["tool_notices"]] == ["ambiguous"]


async def test_copilot_engine_wraps_a_read_resolved_session_read_only(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process engine opens the whole-run bridge itself: a login lent
    only for reading comes back wrapped, so the session lists no write tool
    and a write call through it is refused before it reaches the bridge."""
    from oc8.agent import engine

    inner = _FakeSession()
    opened: list[dict[str, str]] = []

    async def _fake_open(conn: Any, cfg: Any, env: dict[str, str]) -> _FakeSession:
        opened.append(env)
        return inner

    monkeypatch.setattr(engine, "_open_tool_session_for", _fake_open)
    tenant, _anna, depts, conn, run = await _copilot_run(app_session, seats={"Reading": READ_ONLY})
    await _login(app_session, tenant, name="Read", departments={depts["Reading"]})
    async with app_session(tenant) as db:
        run_row = await db.get(m.AgentRun, run)
        conn_row = await db.get(m.McpConnection, conn)
        assert run_row is not None and conn_row is not None
        session = await engine._open_mcp_session(
            db, tenant_id=tenant, mcp_conn=conn_row, run=run_row
        )
        assert isinstance(session, ReadOnlyBridge)
        async with session as entered:
            assert [t.name for t in entered.tools] == ["look"]
            with pytest.raises(ConnectionRequired) as refused:
                await entered.call("change", {})
            assert await entered.call("look", {}) == "ok"
    assert refused.value.reason == "read_only"
    assert inner.calls == ["look"]
    assert opened and opened[0]["LOGIN_URL"] == "https://svc.acme.test"
