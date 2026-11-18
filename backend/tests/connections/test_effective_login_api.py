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
from tests.connections.helpers import api, headers
from tests.connections.test_login_resolver import (  # noqa: F401  (fixture)
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
    await _login(app_session, agent.tenant_id, name="One", departments={agent.department_id})
    await _login(app_session, agent.tenant_id, name="Two", all_departments=True)
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
