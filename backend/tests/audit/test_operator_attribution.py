from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app

pytestmark = pytest.mark.asyncio


async def test_operator_action_attributes_to_the_operator(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        s.add(dept)
        await s.flush()
        dept_id = dept.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="alice", role="org_admin")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                "/api/v1/agents",
                json={"name": "Rep", "departmentId": str(dept_id)},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 201

    async with app_session(tenant) as s:
        ev = (
            (await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == "agent.created")))
            .scalars()
            .first()
        )
        assert ev is not None
        assert ev.responsible_type == "operator" and ev.responsible_id == "alice"


async def test_non_operator_run_records_no_originating_operator(
    app_session: AppSessionFactory,
) -> None:
    """A plugin token can no longer start a run at all.

    This test used to assert the opposite: /run had no role gate, so a plugin
    token reached it, and the guarantee under test was that its subject must NOT
    be recorded as `originating_operator` -- otherwise resolve_responsible
    mislabels it ("operator", subject) in the tamper-evident audit trail.

    That guarantee still holds and still matters as defence in depth
    (`test_resolve_responsible_fallback_chain` covers it directly). What changed
    is that the situation is no longer reachable over HTTP: /run now requires
    `run:start`, a plugin principal carries scopes rather than an operator role,
    and an unknown role holds nothing. The hole that made the old assertion
    necessary is closed rather than merely compensated for -- so this asserts the
    closure, which is the stronger statement."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Rep", status="idle")
        s.add(agent)
        await s.flush()
        agent_id = agent.id
    token = get_identity_provider().mint(
        tenant_id=tenant, subject="plugin-x", role="agent_default", kind="plugin"
    )
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/agents/{agent_id}/run",
                json={"task": "x"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 403, r.text
            # Refused at the run-reach door: a plugin principal stands in no department.
            assert "operator principal" in r.text
