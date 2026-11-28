from __future__ import annotations

import base64
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    # create_credential encrypts field_values under the tenant DEK, which is
    # itself wrapped by this env KEK -- needed only by the subscription-model
    # test below, but harmless (and simplest) as an autouse fixture (mirrors
    # test_triggers_endpoint.py's identical fixture for the same reason).
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def test_admin_can_switch_agent_model_and_receives_current_assignment(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        first = m.ModelConfig(tenant_id=tenant, provider="ollama", model="first", locality="local")
        second = m.ModelConfig(
            tenant_id=tenant, provider="ollama", model="second", locality="local"
        )
        db.add_all([department, first, second])
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name="Switcher",
            model_config_id=first.id,
        )
        db.add(agent)
        await db.flush()
        agent_id, first_id, second_id = agent.id, first.id, second.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            before = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert before.status_code == 200, before.text
            assert before.json()["modelConfigId"] == str(first_id)

            switched = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(second_id)},
                headers=headers,
            )
            assert switched.status_code == 200, switched.text
            assert switched.json()["modelConfigId"] == str(second_id)
            assert switched.json()["llm"] == "second"


async def test_agent_sampling_overrides_persist_independently_and_clear_on_blank(
    app_session: AppSessionFactory,
) -> None:
    """agent.definition["model_params"] -- resolve_params's narrowest-first
    source (modelrouter/sampling.py). Each of the four fields is independent
    (a save touching only one must not disturb the others), and a field
    present but null/blank/empty clears back to "inherit the model's own
    value", matching catalog.py's update_model's own per-field semantics."""
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
            name="Tuned",
            model_config_id=model.id,
        )
        db.add(agent)
        await db.flush()
        agent_id, model_id = agent.id, model.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}

            # Set all four.
            r = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={
                    "modelConfigId": str(model_id),
                    "temperature": 0.9,
                    "maxTokens": 4096,
                    "effort": "high",
                    "extra": {"top_p": 0.9},
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["temperature"] == 0.9
            assert detail.json()["maxTokens"] == 4096
            assert detail.json()["effort"] == "high"
            assert detail.json()["extra"] == {"top_p": 0.9}

            # A save mentioning only temperature must not disturb the rest.
            r = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(model_id), "temperature": 0.5},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["temperature"] == 0.5
            assert detail.json()["maxTokens"] == 4096
            assert detail.json()["effort"] == "high"
            assert detail.json()["extra"] == {"top_p": 0.9}

            # Explicit null/blank/empty on all four clears them back to "inherit".
            r = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={
                    "modelConfigId": str(model_id),
                    "temperature": None,
                    "maxTokens": None,
                    "effort": "",
                    "extra": {},
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["temperature"] is None
            assert detail.json()["maxTokens"] is None
            assert detail.json()["effort"] is None
            assert detail.json()["extra"] is None


async def test_agent_max_steps_override_persists_independently_and_clears_on_zero(
    app_session: AppSessionFactory,
) -> None:
    """agent.definition["max_steps"] -- a top-level key (engine._max_steps),
    unlike the nested model_params sampling fields. Must not disturb a
    sampling override already set, and must clear back to "inherit
    settings.agent_max_steps" on 0/negative, matching the other fields'
    "present but cleared" convention."""
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
            name="Budgeted",
            model_config_id=model.id,
        )
        db.add(agent)
        await db.flush()
        agent_id, model_id = agent.id, model.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}

            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["maxSteps"] is None

            r = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={
                    "modelConfigId": str(model_id),
                    "temperature": 0.7,
                    "maxSteps": 500,
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["maxSteps"] == 500
            assert detail.json()["temperature"] == 0.7

            # A save mentioning only temperature must not disturb max_steps.
            r = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(model_id), "temperature": 0.3},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["maxSteps"] == 500

            # 0 clears the override back to "inherit".
            r = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(model_id), "maxSteps": 0},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["maxSteps"] is None


async def test_agent_run_token_limit_persists_independently_and_clears_on_zero(
    app_session: AppSessionFactory,
) -> None:
    """agent.definition["max_run_tokens"] -- read by oc8.agent.run_limits, a
    top-level key like max_steps, and the same "absent leaves it, 0 clears it"
    convention."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        model = m.ModelConfig(
            tenant_id=tenant, provider="anthropic", model="claude-sonnet-5", locality="cloud"
        )
        db.add_all([department, model])
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=department.id, name="Capped", model_config_id=model.id
        )
        db.add(agent)
        await db.flush()
        agent_id, model_id = agent.id, model.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            url = f"/api/v1/agents/{agent_id}/model-config"
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["maxRunTokens"] is None

            r = await client.patch(
                url,
                json={"modelConfigId": str(model_id), "maxRunTokens": 200_000, "maxSteps": 40},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            r = await client.patch(
                url, json={"modelConfigId": str(model_id), "maxSteps": 50}, headers=headers
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["maxRunTokens"] == 200_000
            assert detail.json()["maxSteps"] == 50

            r = await client.patch(
                url, json={"modelConfigId": str(model_id), "maxRunTokens": 0}, headers=headers
            )
            assert r.status_code == 200, r.text
            detail = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert detail.json()["maxRunTokens"] is None


async def test_non_admin_cannot_switch_agent_model(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        model = m.ModelConfig(tenant_id=tenant, provider="ollama", model="local", locality="local")
        db.add_all([department, model])
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Protected")
        db.add(agent)
        await db.flush()
        agent_id, model_id = agent.id, model.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(model_id)},
                headers={"Authorization": f"Bearer {_token(tenant, 'member')}"},
            )
            assert response.status_code == 403, response.text


# --- Subscription-guard wiring (ChatGPT subscription auth design §5, Task 9's
# assert_manual_only_compatible, mirroring test_triggers_endpoint.py's own
# subscription-guard tests for the trigger side of the same pairing) --------


async def test_switch_model_rejected_when_agent_has_enabled_trigger(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        original = m.ModelConfig(
            tenant_id=tenant, provider="ollama", model="original", locality="local"
        )
        db.add_all([department, original])
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=department.id,
            name="Triggered",
            model_config_id=original.id,
        )
        db.add(agent)
        await db.flush()
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="x",
            cron_expression="0 * * * *",
            enabled=True,
        )
        db.add(trigger)
        await db.flush()
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        db.add(mc)
        await db.flush()
        agent_id, original_id, mc_id = agent.id, original.id, mc.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            resp = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(mc_id)},
                headers=headers,
            )
            assert resp.status_code == 422, resp.text
            assert "ChatGPT subscription" in resp.json()["detail"]

            # The rejected switch must leave the agent's model config
            # genuinely unchanged, not merely return an error while the
            # write already landed.
            after = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
            assert after.status_code == 200, after.text
            assert after.json()["modelConfigId"] == str(original_id)


async def test_switch_model_to_subscription_config_allowed_without_enabled_trigger(
    app_session: AppSessionFactory,
) -> None:
    """The guard only fires when the agent already has an enabled trigger --
    an agent with none must still be able to switch to a subscription model."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Untriggered")
        db.add(agent)
        await db.flush()
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        db.add(mc)
        await db.flush()
        agent_id, mc_id = agent.id, mc.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            resp = await client.patch(
                f"/api/v1/agents/{agent_id}/model-config",
                json={"modelConfigId": str(mc_id)},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["modelConfigId"] == str(mc_id)
