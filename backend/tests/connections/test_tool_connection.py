"""One connection row per tool; whether an agent or a person reaches it is
the policy's and the login resolver's decision, not the row's department
(unified connections design, "Storage").

Built on the throwaway tool pack of `test_login_resolver`, so no vendor is
involved."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.api.mcp_gateway import _connections
from oc8.authz.pdp import effective_tool_policies
from oc8.capas.discovery import find_plugin
from oc8.connections.access import (
    ConnectionRequired,
    binding_fingerprint,
    resolve_direct_access,
    resolve_tool_access,
)
from oc8.connections.runtime_env import integration_connections
from oc8.connections.tool_connection import tool_connection
from oc8.connections.usable import department_systems, member_may_use
from tests.conftest import AppSessionFactory
from tests.connections.helpers import enable_plugin_row, member
from tests.connections.test_login_resolver import (  # noqa: F401 -- `_test_pack` is autouse
    CFG,
    FRAME,
    KEY,
    PLUGIN,
    _department,
    _login,
    _test_pack,
)
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio


async def _install(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    """The test pack installed and enabled (rows only, with its manifest)."""
    found = find_plugin(PLUGIN)
    assert found is not None and found.manifest is not None
    async with app_session(tenant) as db:
        capa = m.Capa(tenant_id=tenant, name=PLUGIN, type="tool_pack", trust_level="first_party")
        db.add(capa)
        await db.flush()
        version = m.CapaVersion(
            tenant_id=tenant,
            capa_id=capa.id,
            semver="1.0.0",
            manifest=dict(found.manifest),
            artifact_hash=b"",
            permissions=[],
            capabilities=[],
            entry_points={},
        )
        db.add(version)
        await db.flush()
        capa.current_version_id = version.id
        db.add(
            m.CapaInstallation(
                tenant_id=tenant, capa_id=capa.id, status="enabled", granted_permissions=[]
            )
        )


async def _row(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    *,
    department_id: uuid.UUID | None = None,
    env: dict[str, str] | None = None,
) -> uuid.UUID:
    async with app_session(tenant) as db:
        conn = m.McpConnection(
            tenant_id=tenant,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            config={**CFG, "env": {**CFG["env"], **(env or {})}},
            department_id=department_id,
        )
        db.add(conn)
        await db.flush()
        return conn.id


async def _agent_in(
    app_session: AppSessionFactory, tenant: uuid.UUID, department_id: uuid.UUID
) -> m.Agent:
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=department_id, name="Second")
        db.add(agent)
        await db.flush()
        return agent


async def _world(app_session: AppSessionFactory) -> tuple[m.Agent, uuid.UUID]:
    """An agent whose department enables the tool, and a second department
    (same frame). The plugin is installed and enabled."""
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = FRAME
    other = await _department(app_session, agent.tenant_id, "Other", FRAME)
    await _install(app_session, agent.tenant_id)
    return agent, other


async def _offered(app_session: AppSessionFactory, agent: m.Agent) -> list[uuid.UUID]:
    """The rows `integration_connections` hands a run of `agent`."""
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        dept = await db.get(m.Department, agent.department_id)
        assert live is not None and dept is not None
        policies = effective_tool_policies(dept.frame or {}, live.narrowing or {})
        return [
            c.id
            for c in await integration_connections(db, agent=live, policies=policies, have=set())
        ]


async def _run_connections(app_session: AppSessionFactory, agent: m.Agent) -> list[uuid.UUID]:
    """What the MCP gateway hands a run of `agent`."""
    async with app_session(agent.tenant_id) as db:
        run = m.AgentRun(tenant_id=agent.tenant_id, agent_id=agent.id, source="manual", context={})
        db.add(run)
        await db.flush()
        dept = await db.get(m.Department, agent.department_id)
        return [c.id for c in await _connections(db, run, dept)]


async def _seat(
    app_session: AppSessionFactory, tenant: uuid.UUID, member_id: uuid.UUID, dept: uuid.UUID
) -> None:
    async with app_session(tenant) as db:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant, member_id=member_id, department_id=dept, seat_role="dept_viewer"
            )
        )


async def _may_use(app_session: AppSessionFactory, tenant: uuid.UUID, who: uuid.UUID) -> bool:
    async with app_session(tenant) as db:
        return await member_may_use(db, member_id=who, tool_key=KEY, right="read")


async def _systems(
    app_session: AppSessionFactory, tenant: uuid.UUID, department_id: uuid.UUID
) -> dict[str, tuple[bool, str | None]]:
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, department_id)
        assert dept is not None
        return {
            s.key: (s.usable, s.missing)
            for s in await department_systems(db, tenant_id=tenant, department=dept)
        }


# ------------------------------------------------------------------ tests


async def test_an_agent_reaches_the_shared_tool_row(app_session: AppSessionFactory) -> None:
    agent, other = await _world(app_session)
    tenant = agent.tenant_id
    shared = await _row(app_session, tenant)
    # Two organisation logins: only enablement decides, not "the single one".
    await _login(app_session, tenant, name="Everyone", all_departments=True)
    await _login(app_session, tenant, name="Spare")
    second = await _agent_in(app_session, tenant, other)

    async with app_session(tenant) as db:
        for dept in (agent.department_id, other):
            row = await tool_connection(db, name=KEY, department_id=dept)
            assert row is not None and row.id == shared
    for who in (agent, second):
        assert await _offered(app_session, who) == [shared]
        assert shared in await _run_connections(app_session, who)


async def test_unconsolidated_tenant_still_uses_its_department_row(
    app_session: AppSessionFactory,
) -> None:
    agent, other = await _world(app_session)
    tenant = agent.tenant_id
    shared = await _row(app_session, tenant)
    own = await _row(
        app_session,
        tenant,
        department_id=agent.department_id,
        env={"LOGIN_URL": "https://copied.acme.test"},
    )
    await _login(app_session, tenant, name="Other's", departments={other})

    async with app_session(tenant) as db:
        mine = await tool_connection(db, name=KEY, department_id=agent.department_id)
        theirs = await tool_connection(db, name=KEY, department_id=other)
        assert mine is not None and mine.id == own
        assert theirs is not None and theirs.id == shared
    # The department's run still gets its own row and acts through the
    # values copied into it.
    assert own in await _run_connections(app_session, agent)
    assert shared not in await _run_connections(app_session, agent)
    async with app_session(tenant) as db:
        row = await db.get(m.McpConnection, own)
        live = await db.get(m.Agent, agent.id)
        assert row is not None and live is not None
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="manual", context={})
        db.add(run)
        await db.flush()
        access = await resolve_tool_access(db, run=run, agent=live, conn=row)
    assert access.kind == "legacy"


async def test_a_tool_without_a_usable_login_is_not_offered_to_copilot(
    app_session: AppSessionFactory,
) -> None:
    agent, _other = await _world(app_session)
    tenant = agent.tenant_id
    await _row(app_session, tenant)
    anna = await member(app_session, tenant, "anna@acme.test")
    await _seat(app_session, tenant, anna, agent.department_id)
    # Two organisation logins, none enabled for Anna's department: a call
    # would be refused as ambiguous, so the Copilot must not offer it.
    first = await _login(app_session, tenant, name="One")
    await _login(app_session, tenant, name="Two")
    assert not await _may_use(app_session, tenant, anna)

    async with app_session(tenant) as db:
        cred = await db.get(m.Credential, first)
        assert cred is not None
        db.add(
            m.CredentialDepartment(
                tenant_id=tenant, credential_id=cred.id, department_id=agent.department_id
            )
        )
    assert await _may_use(app_session, tenant, anna)


async def test_a_department_with_one_enabled_login_among_several_is_offered_the_tool(
    app_session: AppSessionFactory,
) -> None:
    agent, other = await _world(app_session)
    tenant = agent.tenant_id
    shared = await _row(app_session, tenant)
    await _login(app_session, tenant, name="Ours", departments={agent.department_id})
    await _login(app_session, tenant, name="Not enabled")
    anna = await member(app_session, tenant, "anna@acme.test")
    await _seat(app_session, tenant, anna, agent.department_id)
    bob = await member(app_session, tenant, "bob@acme.test")
    await _seat(app_session, tenant, bob, other)
    second = await _agent_in(app_session, tenant, other)

    # The agent's department has exactly one enabled login: offered,
    # to the agent, to the builder and to the Copilot of a person seated there.
    assert await _offered(app_session, agent) == [shared]
    assert (await _systems(app_session, tenant, agent.department_id))[KEY] == (True, None)
    assert await _may_use(app_session, tenant, anna)

    # The other department has none enabled and the organisation more than
    # one: not offered anywhere -- the resolver would refuse the call.
    assert await _offered(app_session, second) == []
    usable, missing = (await _systems(app_session, tenant, other))[KEY]
    assert usable is False and missing is not None and "/connections" in missing
    assert not await _may_use(app_session, tenant, bob)


# ------------------------------------------------------------------ fix round 1

OAUTH_KEY = "microsoft365"
OAUTH_CFG: dict[str, Any] = {
    "_plugin_name": "microsoft365",
    "_connection_key": "primary",
    "command": "python",
    "args": ["-m", "mcp_bridge"],
    "env": {},
    "secret_env": {"GRAPH_ACCESS_TOKEN": "oauth:graph_access_token"},
}


async def test_copilot_still_offers_an_oauth_tool_before_sign_in(
    app_session: AppSessionFactory,
) -> None:
    """A sign-in integration is offered everywhere before the person signed
    in -- the sign-in prompt at the first call is how they connect."""
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    dept = await _department(
        app_session,
        tenant,
        "Office",
        {"tools": {OAUTH_KEY: {"enabled": True, "read": True, "modify": True}}},
    )
    await _seat(app_session, tenant, anna, dept)
    async with app_session(tenant) as db:
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                name=OAUTH_KEY,
                server_url="",
                transport="stdio",
                connected=True,
                config=dict(OAUTH_CFG),
            )
        )
    await enable_plugin_row(app_session, tenant, "microsoft365")
    async with app_session(tenant) as db:
        assert await member_may_use(db, member_id=anna, tool_key=OAUTH_KEY, right="read")


async def test_copilot_reports_ambiguous_logins_not_not_connected(
    app_session: AppSessionFactory,
) -> None:
    agent, _other = await _world(app_session)
    tenant = agent.tenant_id
    conn_id = await _row(app_session, tenant)
    anna = await member(app_session, tenant, "anna@acme.test")
    await _seat(app_session, tenant, anna, agent.department_id)
    await _login(app_session, tenant, name="One", departments={agent.department_id})
    await _login(app_session, tenant, name="Two", departments={agent.department_id})
    assert not await _may_use(app_session, tenant, anna)
    async with app_session(tenant) as db:
        conn = await db.get(m.McpConnection, conn_id)
        assert conn is not None
        with pytest.raises(ConnectionRequired) as exc:
            await resolve_direct_access(db, member_id=anna, conn=conn, right="read")
    assert exc.value.reason == "ambiguous"


#: Fixed ids, so the fingerprint is reproducible across test databases.
_FP_OWN = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
_FP_PINNED = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
_FP_SHARED = uuid.UUID("00000000-0000-4000-8000-0000000000a3")
_FP_LOGIN = uuid.UUID("00000000-0000-4000-8000-0000000000b1")
_FP_VERSION = uuid.UUID("00000000-0000-4000-8000-0000000000c1")

#: What `binding_fingerprint` of base 6b9237e7 (before one row per tool)
#: returned for this fixture: (every tool, one tool).
_FP_BASE_ALL = "e4d2d68dbf8bf1420bf8c510198c556feda474316cb6f8043a759beb0ad6162d"
_FP_BASE_ONE = "4becd1adc5c8eb9defa00d995975b65ebdb0757b05300e746a06c26d1764f999"


async def test_binding_fingerprint_is_unchanged_for_an_unconsolidated_tenant(
    app_session: AppSessionFactory,
) -> None:
    """Stored confirmations (a trigger's `access_fingerprint`, a binding's)
    stay valid on deploy: the same inputs give the same digest as before."""
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.goal = "Sell things"
        dept.frame = FRAME
        dept.frame_capa_defaults = {}
        common: dict[str, Any] = {
            "tenant_id": tenant,
            "server_url": "",
            "transport": "stdio",
            "connected": True,
            "scopes": [],
        }
        db.add_all(
            [
                m.McpConnection(
                    id=_FP_OWN,
                    name=KEY,
                    department_id=agent.department_id,
                    config={**CFG, "env": {**CFG["env"], "LOGIN_URL": "https://copied.test"}},
                    **common,
                ),
                m.McpConnection(
                    id=_FP_PINNED,
                    name="pinned_tool",
                    department_id=agent.department_id,
                    credential_id=_FP_LOGIN,
                    config={"command": "x", "args": []},
                    **common,
                ),
                # Department-less (an installed integration's own row): never
                # part of the department's setup before, and not now.
                m.McpConnection(id=_FP_SHARED, name="other_tool", config=dict(CFG), **common),
            ]
        )
    async with app_session(tenant) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        every = await binding_fingerprint(db, live, None, version_id=_FP_VERSION)
        one = await binding_fingerprint(db, live, KEY, version_id=_FP_VERSION)
    assert (every, one) == (_FP_BASE_ALL, _FP_BASE_ONE)
