from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.chat.model_choice import allowed_model_ids, is_allowed_override, model_options
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, cloud_chat_enabled: bool = True
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        cloud = m.ModelConfig(
            tenant_id=tenant,
            provider="openai",
            model="gpt-x",
            locality="cloud",
            display_name="GPT X",
            chat_enabled=cloud_chat_enabled,
        )
        local = m.ModelConfig(
            tenant_id=tenant,
            provider="ollama",
            model="llama",
            locality="local",
            display_name="Llama",
        )
        auto = m.ModelConfig(tenant_id=tenant, provider="auto", model="router", locality="cloud")
        db.add_all([cloud, local, auto])
        await db.flush()
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A", model_config_id=cloud.id)
        db.add(agent)
        await db.flush()
        return agent.id, cloud.id, local.id, auto.id


async def test_every_chat_enabled_concrete_model_is_allowed(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, cloud, local, _auto = await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert await allowed_model_ids(db, agent=agent) == frozenset({cloud, local})


async def test_chat_disabled_model_is_rejected(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, cloud, local, _auto = await _seed(app_session, tenant, cloud_chat_enabled=False)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert await allowed_model_ids(db, agent=agent) == frozenset({local})
        assert await is_allowed_override(db, agent=agent, model_config_id=local)
        assert not await is_allowed_override(db, agent=agent, model_config_id=cloud)
        assert not await is_allowed_override(db, agent=agent, model_config_id=uuid.uuid4())


async def test_options_put_auto_first_and_mark_the_default(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, cloud, local, _auto = await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        options = await model_options(db, agent=agent, default_model_id=cloud)
    assert options[0].id is None and options[0].label == "Auto"
    by_id = {o.id: o for o in options[1:]}
    assert by_id[str(cloud)].is_default is True
    assert by_id[str(local)].locality == "local"
    assert len(options) == 3


async def test_options_leave_out_chat_disabled_models(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, cloud, local, _auto = await _seed(app_session, tenant, cloud_chat_enabled=False)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        options = await model_options(db, agent=agent, default_model_id=cloud)
    assert [o.id for o in options] == [None, str(local)]


async def test_other_tenants_models_are_never_allowed(app_session: AppSessionFactory) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    agent_id, *_ = await _seed(app_session, tenant)
    _, foreign, _, _ = await _seed(app_session, other)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert not await is_allowed_override(db, agent=agent, model_config_id=foreign)
