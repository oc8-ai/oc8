"""`model_params.reasoning` is the chat's level and is set only at run time;
saving an agent's model settings drops it from the stored definition."""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.api.test_agent_model_switch import _token
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_saving_model_settings_strips_a_stored_reasoning_level(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        model = m.ModelConfig(
            tenant_id=tenant, provider="anthropic", model="claude-sonnet-5", locality="cloud"
        )
        db.add_all([department, model])
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name="Stale",
            model_config_id=model.id,
            definition={"model_params": {"temperature": 0.4, "reasoning": "thorough"}},
        )
        db.add(agent)
        await db.flush()
        agent_id, model_id = agent.id, model.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(model_id), "maxTokens": 1000},
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        stored = await db.get(m.Agent, agent_id)
        assert stored is not None
        assert stored.definition["model_params"] == {"temperature": 0.4, "max_tokens": 1000}
