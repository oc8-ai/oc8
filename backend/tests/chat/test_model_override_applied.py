"""A chat's model override reaches every runtime through `resolve_version`,
and a refused one leaves exactly one audit line (spec §2.3)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import pinned_model_config_id, resolve_version
from oc8.chat.model_choice import audit_model_override
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, allow_override: bool
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        base = m.ModelConfig(tenant_id=tenant, provider="ollama", model="llama", locality="local")
        other = m.ModelConfig(
            tenant_id=tenant,
            provider="openai",
            model="gpt-x",
            locality="cloud",
            chat_enabled=allow_override,
        )
        db.add_all([base, other])
        await db.flush()
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A", model_config_id=base.id)
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="chat",
            context={"model_override_id": str(other.id)},
        )
        db.add(run)
        await db.flush()
        return agent.id, run.id, base.id, other.id


async def _rejections(db: AsyncSession) -> int:
    rows = await db.execute(
        select(m.AuditEvent).where(m.AuditEvent.action == "chat.model_override_rejected")
    )
    return len(rows.scalars().all())


async def test_allowed_override_replaces_the_pinned_model(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, run_id, _base, other = await _seed(app_session, tenant, allow_override=True)
    async with app_session(tenant) as db:
        agent, run = await db.get(m.Agent, agent_id), await db.get(m.AgentRun, run_id)
        assert agent is not None and run is not None
        payload = await resolve_version(db, run, agent)
        assert pinned_model_config_id(payload) == other
        await audit_model_override(db, run=run, agent=agent)
        assert await _rejections(db) == 0


async def test_override_not_chat_enabled_falls_back_and_audits_once(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, run_id, base, _other = await _seed(app_session, tenant, allow_override=False)
    async with app_session(tenant) as db:
        agent, run = await db.get(m.Agent, agent_id), await db.get(m.AgentRun, run_id)
        assert agent is not None and run is not None
        payload = await resolve_version(db, run, agent)
        assert pinned_model_config_id(payload) == base
        await audit_model_override(db, run=run, agent=agent)
        await audit_model_override(db, run=run, agent=agent)
        assert await _rejections(db) == 1


async def test_deleted_override_falls_back_and_audits_once(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, run_id, base, other = await _seed(app_session, tenant, allow_override=True)
    async with app_session(tenant) as db:
        cfg = await db.get(m.ModelConfig, other)
        assert cfg is not None
        await db.delete(cfg)
        await db.flush()
        agent, run = await db.get(m.Agent, agent_id), await db.get(m.AgentRun, run_id)
        assert agent is not None and run is not None
        assert pinned_model_config_id(await resolve_version(db, run, agent)) == base
        await audit_model_override(db, run=run, agent=agent)
        assert await _rejections(db) == 1


async def test_a_run_without_override_is_untouched(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, run_id, base, _ = await _seed(app_session, tenant, allow_override=True)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        agent = await db.get(m.Agent, agent_id)
        assert run is not None and agent is not None
        run.context = {}
        await db.flush()
        assert pinned_model_config_id(await resolve_version(db, run, agent)) == base
        await audit_model_override(db, run=run, agent=agent)
        assert await _rejections(db) == 0
