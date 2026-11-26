from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.hire import (
    create_hire_request,
    require_hire_approval,
    resolve_hire_agent,
    set_require_hire_approval,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _org(s: AsyncSession, tenant: uuid.UUID, **settings: Any) -> m.Organization:
    org = m.Organization(
        id=tenant, slug=f"t{tenant.hex[:6]}", name="T", tier="standard", region="eu",
        settings=dict(settings),
    )
    s.add(org)
    await s.flush()
    return org


def _agent(tenant: uuid.UUID, status: str = "pending_approval") -> m.Agent:
    return m.Agent(
        tenant_id=tenant, department_id=uuid.uuid4(), name="New", role_title="Rep",
        mission="help", is_team_lead=False, status=status,
    )


async def test_require_flag_defaults_false_and_reads_after_set(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        # No org row -> default False.
        assert await require_hire_approval(s, tenant_id=tenant) is False
        await _org(s, tenant)
        assert await require_hire_approval(s, tenant_id=tenant) is False
        await set_require_hire_approval(s, tenant_id=tenant, enabled=True)
    # Read in a FRESH session: a genuine DB round-trip, so an in-place JSONB
    # mutation (untracked -> never flushed) would read back False and fail this.
    async with app_session(tenant) as s:
        assert await require_hire_approval(s, tenant_id=tenant) is True


async def test_create_hire_request_snapshots_config(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        agent = _agent(tenant)
        s.add(agent)
        await s.flush()
        req = await create_hire_request(s, agent=agent)
        assert req.action_type == "hire_agent" and req.status == "pending"
        assert req.agent_id == agent.id
        assert req.payload["name"] == "New"
        assert req.payload["department_id"] == str(agent.department_id)
        assert "Hire agent" in req.title


async def test_resolve_approve_activates_and_reject_soft_deletes(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        a1, a2 = _agent(tenant), _agent(tenant)
        s.add_all([a1, a2])
        await s.flush()
        r1 = await create_hire_request(s, agent=a1)
        r2 = await create_hire_request(s, agent=a2)
        await resolve_hire_agent(s, approval_request=r1, decision="approve")
        await resolve_hire_agent(s, approval_request=r2, decision="reject")
        assert a1.status == "idle" and a1.deleted_at is None
        assert a2.deleted_at is not None
