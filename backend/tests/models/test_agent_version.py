"""`agent_version`: an immutable snapshot of an agent's behavioural config.

Runs pin `agent_run.agent_version_id`, so publishing never changes what an
in-flight run is doing -- the same rule `SkillAssignment` already applies to
skill versions. This file tests only the table shape and its uniqueness
constraint; the snapshot/publish functions are tested in
`tests/agents/test_agent_versioning.py`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_agent_version_is_tenant_scoped_and_unique_per_number(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="New")
        db.add(agent)
        await db.flush()

        v1 = m.AgentVersion(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            version_no=1,
            payload={"mission": "a"},
            payload_hash=b"\x00" * 32,
        )
        db.add(v1)
        await db.flush()

        dup = m.AgentVersion(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            version_no=1,
            payload={"mission": "b"},
            payload_hash=b"\x01" * 32,
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_agent_gains_a_current_version_id_column(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="New")
        db.add(agent)
        await db.flush()
        assert agent.current_version_id is None

        version = m.AgentVersion(
            tenant_id=tenant,
            agent_id=agent.id,
            version_no=1,
            payload={},
            payload_hash=b"\x00" * 32,
        )
        db.add(version)
        await db.flush()
        agent.current_version_id = version.id
        await db.flush()
    async with app_session(tenant) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        assert a.current_version_id == version.id


async def test_agent_run_gains_an_agent_version_id_column(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="New")
        db.add(agent)
        await db.flush()
        version = m.AgentVersion(
            tenant_id=tenant,
            agent_id=agent.id,
            version_no=1,
            payload={},
            payload_hash=b"\x00" * 32,
        )
        db.add(version)
        await db.flush()

        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, agent_version_id=version.id)
        db.add(run)
        await db.flush()
    async with app_session(tenant) as db:
        r = await db.get(m.AgentRun, run.id)
        assert r is not None
        assert r.agent_version_id == version.id


async def test_agent_run_agent_version_id_is_nullable(app_session: AppSessionFactory) -> None:
    """Historical runs from before this migration have no version to point at --
    a reader must treat null as 'read the live agent row', not fail to load."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="New")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id)
        db.add(run)
        await db.flush()
        assert run.agent_version_id is None
