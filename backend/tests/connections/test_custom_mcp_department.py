"""A custom MCP server registered through the wizard reaches the agents of
the department it was set up for -- and the builder never calls a connection
usable that the gateway would not hand to a run (one shared rule,
`connections.usable`)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.api.mcp_gateway import _connections
from oc8.connections.usable import department_systems
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers

pytestmark = pytest.mark.asyncio

NAME = "acme_tickets"
FRAME = {"tools": {NAME: {"enabled": True, "read": True, "modify": True}}}


def _manifest(*, with_department: bool) -> dict[str, Any]:
    """The shape custom-mcp-wizard.tsx's buildManifest() sends."""
    fields: list[dict[str, Any]] = []
    mcp: dict[str, Any] = {
        "connection_key": "default",
        "name": NAME,
        "command": "",
        "args": [],
        "env_fields": {},
        "env": {},
        "secret_env_fields": {},
    }
    if with_department:
        fields.append({"key": "department", "label": "Department", "kind": "department"})
        mcp["department_field"] = "department"
    return {
        "name": NAME,
        "version": "1.0.0",
        "type": "tool_pack",
        "summary": "Acme's ticket system.",
        "tool_pack": {
            "connections": [
                {
                    "key": "default",
                    "name": NAME,
                    "server_url": "http://tickets.acme.test/mcp",
                    "transport": "http",
                    "config": {},
                }
            ]
        },
        "setup": {"title": NAME, "fields": fields, "mcp": mcp},
    }


async def _departments(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        support = m.Department(tenant_id=tenant, name="Support", frame=FRAME)
        sales = m.Department(tenant_id=tenant, name="Sales", frame=FRAME)
        db.add_all([support, sales])
        await db.flush()
        return support.id, sales.id


async def _install_and_setup(
    tenant: uuid.UUID, values: dict[str, str], *, with_department: bool = True
) -> tuple[int, Any]:
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        r = await c.post(
            "/api/v1/capas",
            json={"manifest": _manifest(with_department=with_department), "origin": "custom"},
            headers=admin,
        )
        assert r.status_code == 201, r.text
        capa_id = r.json()["pluginId"]
        r = await c.post(
            f"/api/v1/capas/{capa_id}/enable", json={"grantedPermissions": []}, headers=admin
        )
        assert r.status_code == 200, r.text
        r = await c.post(f"/api/v1/capas/{capa_id}/setup", json={"values": values}, headers=admin)
        return r.status_code, r.json()


async def _mark_connected(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    """What a passing connection test leaves behind."""
    async with app_session(tenant) as db:
        for conn in (await db.execute(select(m.McpConnection))).scalars():
            conn.connected = True


async def _run_connections(
    app_session: AppSessionFactory, tenant: uuid.UUID, department: uuid.UUID
) -> list[str]:
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=department, name="Tia")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="manual", context={})
        db.add(run)
        await db.flush()
        dept = await db.get(m.Department, department)
        return [c.name for c in await _connections(db, run, dept)]


async def _systems(
    app_session: AppSessionFactory, tenant: uuid.UUID, department: uuid.UUID
) -> dict[str, tuple[bool, str | None]]:
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, department)
        assert dept is not None
        return {
            s.key: (s.usable, s.missing)
            for s in await department_systems(db, tenant_id=tenant, department=dept)
        }


async def test_the_wizard_connection_lands_in_the_chosen_department_and_reaches_its_runs(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    support, sales = await _departments(app_session, tenant)
    code, body = await _install_and_setup(tenant, {"department": str(support)})
    assert code == 200, body
    async with app_session(tenant) as db:
        conn = await db.get(m.McpConnection, uuid.UUID(body["connectionId"]))
        assert conn is not None and conn.department_id == support
    await _mark_connected(app_session, tenant)

    assert NAME in await _run_connections(app_session, tenant, support)
    assert await _systems(app_session, tenant, support) == {NAME: (True, None)}
    # Another department neither gets it nor is told it has it.
    assert NAME not in await _run_connections(app_session, tenant, sales)
    assert NAME not in await _systems(app_session, tenant, sales)


async def test_a_connection_in_no_department_is_not_reported_usable(
    app_session: AppSessionFactory,
) -> None:
    """Created before the wizard asked for a department: the gateway hands it
    to no run, so the builder must not offer it -- it says what is missing."""
    tenant = uuid.uuid4()
    support, _sales = await _departments(app_session, tenant)
    code, body = await _install_and_setup(tenant, {}, with_department=False)
    assert code == 200, body
    await _mark_connected(app_session, tenant)

    assert NAME not in await _run_connections(app_session, tenant, support)
    usable, missing = (await _systems(app_session, tenant, support))[NAME]
    assert usable is False
    assert missing is not None and "no department" in missing


async def test_setup_refuses_another_organisations_department(
    app_session: AppSessionFactory,
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    await _departments(app_session, tenant)
    foreign, _ = await _departments(app_session, other)
    code, _body = await _install_and_setup(tenant, {"department": str(foreign)})
    assert code == 404
    async with app_session(tenant) as db:
        rows = (await db.execute(select(m.McpConnection))).scalars().all()
        assert all(row.department_id is None for row in rows)


async def test_setup_refuses_an_archived_department(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    support, _sales = await _departments(app_session, tenant)
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, support)
        assert dept is not None
        dept.deleted_at = dt.datetime.now(tz=dt.UTC)
    code, _body = await _install_and_setup(tenant, {"department": str(support)})
    assert code == 404


async def test_an_existing_connection_can_be_moved_into_a_department(
    app_session: AppSessionFactory,
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    support, _sales = await _departments(app_session, tenant)
    foreign, _ = await _departments(app_session, other)
    code, body = await _install_and_setup(tenant, {}, with_department=False)
    assert code == 200, body
    await _mark_connected(app_session, tenant)
    conn_id = body["connectionId"]
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with api() as c:
        listed = await c.get("/api/v1/mcp/connections", headers=admin)
        assert listed.status_code == 200, listed.text
        items = listed.json()
        items = items["items"] if isinstance(items, dict) else items
        row = next(i for i in items if i["id"] == conn_id)
        assert row["needsDepartment"] is True

        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}",
            json={"departmentId": str(foreign)},
            headers=admin,
        )
        assert r.status_code == 404, r.text

        r = await c.patch(
            f"/api/v1/mcp/connections/{conn_id}",
            json={"departmentId": str(support)},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        moved = r.json()
        assert moved["departmentId"] == str(support)
        assert moved["needsDepartment"] is False
        # Only where it lives changed, not what it runs: its tested state stays.
        assert moved["connected"] is True

    assert NAME in await _run_connections(app_session, tenant, support)
    assert await _systems(app_session, tenant, support) == {NAME: (True, None)}
