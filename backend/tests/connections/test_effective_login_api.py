"""`GET /agents/{id}/tool-access/{key}/effective`: which login the agent's
tool uses for the person looking, and why (unified connections design,
"effective login visible").

Built on the throwaway tool pack and login type of the resolver tests, so no
vendor is involved."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.capas.discovery import find_plugin
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers, member
from tests.connections.test_login_resolver import (  # noqa: F401  (fixture)
    CFG,
    FRAME,
    KEY,
    PLUGIN,
    _department,
    _login,
    _test_pack,
)
from tests.connections.test_login_resolver import _world as _resolver_world

pytestmark = pytest.mark.asyncio

ADMIN = "org_admin"


async def _world(
    app_session: AppSessionFactory,
) -> tuple[m.Agent, uuid.UUID, uuid.UUID, uuid.UUID]:
    """The resolver tests' world, with the pack's manifest on its capa row
    so the integration is listed as available."""
    world = await _resolver_world(app_session)
    tenant = world[0].tenant_id
    found = find_plugin(PLUGIN)
    assert found is not None and found.manifest is not None
    async with app_session(tenant) as db:
        capa = await db.scalar(select(m.Capa).where(m.Capa.name == PLUGIN))
        assert capa is not None
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
    return world


async def _effective(agent: m.Agent) -> dict[str, object]:
    async with api() as c:
        r = await c.get(
            f"/api/v1/agents/{agent.id}/tool-access/{KEY}/effective",
            headers=headers(agent.tenant_id, "root@acme.test", ADMIN),
        )
    assert r.status_code == 200, r.text
    body: dict[str, object] = r.json()
    return body


async def test_effective_login_names_the_source(app_session: AppSessionFactory) -> None:
    agent, _anna, _conn, _run = await _world(app_session)
    other = await _department(app_session, agent.tenant_id, "Other", FRAME)
    await _login(app_session, agent.tenant_id, name="Other's", departments={other})
    ours = await _login(
        app_session, agent.tenant_id, name="Sales login", departments={agent.department_id}
    )
    assert await _effective(agent) == {
        "credentialId": str(ours),
        "credentialName": "Sales login",
        "source": "department",
        "reason": None,
        "missing": [],
    }


async def test_effective_login_reports_ambiguous(app_session: AppSessionFactory) -> None:
    agent, _anna, _conn, _run = await _world(app_session)
    # Two of the same tier (a department's own choice wins over a login
    # enabled for all departments).
    await _login(app_session, agent.tenant_id, name="One", departments={agent.department_id})
    await _login(app_session, agent.tenant_id, name="Two", departments={agent.department_id})
    assert await _effective(agent) == {
        "credentialId": None,
        "credentialName": None,
        "source": None,
        "reason": "ambiguous",
        "missing": [],
    }


async def test_effective_login_shows_the_agents_choice(app_session: AppSessionFactory) -> None:
    agent, _anna, _conn, _run = await _world(app_session)
    await _login(app_session, agent.tenant_id, name="One", departments={agent.department_id})
    await _login(app_session, agent.tenant_id, name="Two", all_departments=True)
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
    body = await _effective(agent)
    assert (body["credentialId"], body["source"], body["reason"]) == (
        str(chosen),
        "agent",
        None,
    )


async def test_effective_login_names_what_an_incomplete_login_lacks(
    app_session: AppSessionFactory,
) -> None:
    agent, _anna, _conn, _run = await _world(app_session)
    empty = await _login(
        app_session, agent.tenant_id, name="Empty", url="", departments={agent.department_id}
    )
    assert await _effective(agent) == {
        "credentialId": str(empty),
        "credentialName": "Empty",
        "source": "department",
        "reason": "incomplete",
        "missing": ["url"],
    }


async def test_effective_login_without_any_login(app_session: AppSessionFactory) -> None:
    agent, _anna, _conn, _run = await _world(app_session)
    body = await _effective(agent)
    assert (body["credentialId"], body["source"], body["reason"]) == (
        None,
        None,
        "not_configured",
    )


async def test_effective_login_matches_the_runtime_for_a_pinned_legacy_connection(
    app_session: AppSessionFactory,
) -> None:
    """An agent pinned to an older login row (`/mcp/logins`, unconsolidated)
    runs on that login -- even when the person looking has their own."""
    agent, _anna, _conn, _run = await _world(app_session)
    root = await member(app_session, agent.tenant_id, "root@acme.test")
    await _login(app_session, agent.tenant_id, name="Mine", owner=root)
    pinned = await _login(app_session, agent.tenant_id, name="Pinned")
    async with app_session(agent.tenant_id) as db:
        row = m.McpConnection(
            tenant_id=agent.tenant_id,
            name=KEY,
            server_url="",
            transport="stdio",
            connected=True,
            config=dict(CFG),
            credential_id=pinned,
        )
        db.add(row)
        await db.flush()
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        live.narrowing = {"tools": {KEY: {"enabled": True, "connection_id": str(row.id)}}}
    body = await _effective(agent)
    assert (body["credentialId"], body["credentialName"], body["source"], body["reason"]) == (
        str(pinned),
        "Pinned",
        "legacy",
        None,
    )


async def test_effective_login_reports_a_refused_agent_choice(
    app_session: AppSessionFactory,
) -> None:
    """A fixed choice of a person's own login nobody confirmed: the runtime
    refuses it, so the line names that instead of the login."""
    agent, anna, _conn, _run = await _world(app_session)
    await _login(app_session, agent.tenant_id, name="Dept", departments={agent.department_id})
    annas = await _login(app_session, agent.tenant_id, name="Anna's", owner=anna)
    async with app_session(agent.tenant_id) as db:
        db.add(
            m.AgentToolAccess(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                tool_key=KEY,
                mode="fixed",
                credential_id=annas,
                set_with_manage=False,
                set_by_member_id=None,
            )
        )
    assert await _effective(agent) == {
        "credentialId": None,
        "credentialName": None,
        "source": None,
        "reason": "binding_needs_confirmation",
        "missing": [],
    }


async def test_effective_login_needs_rights_on_the_agent(app_session: AppSessionFactory) -> None:
    agent, _anna, _conn, _run = await _world(app_session)
    async with api() as c:
        r = await c.get(
            f"/api/v1/agents/{agent.id}/tool-access/{KEY}/effective",
            headers=headers(agent.tenant_id, "nobody@acme.test", "member"),
        )
    assert r.status_code in (403, 404), r.text
