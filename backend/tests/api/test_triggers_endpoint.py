from __future__ import annotations

import base64
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID, GLOBEX_TENANT_ID
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    # create_credential encrypts field_values under the tenant DEK, which is
    # itself wrapped by this env KEK -- needed only by the subscription-model
    # tests below, but harmless (and simplest) as an autouse fixture.
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


async def _agent_and_headers(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, dict[str, str]]:
    async with app_session(tenant) as s:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add(agent)
        await s.flush()
        agent_id = agent.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    return agent_id, {"Authorization": f"Bearer {token}"}


async def _subscription_agent_and_headers(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, dict[str, str]]:
    """An agent bound to a ChatGPT-subscription-backed model."""
    async with app_session(tenant) as s:
        cred = await create_credential(
            s,
            tenant_id=tenant,
            name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        s.add(mc)
        await s.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Subscription-bound",
            model_config_id=mc.id,
        )
        s.add(agent)
        await s.flush()
        agent_id = agent.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    return agent_id, {"Authorization": f"Bearer {token}"}


async def test_create_list_update_delete_cron_trigger(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, headers = await _agent_and_headers(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            create = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "cron", "taskText": "daily report", "cronExpression": "0 9 * * *"},
                headers=headers,
            )
            assert create.status_code == 201
            body = create.json()
            trigger_id = body["id"]
            assert body["kind"] == "cron"
            assert body["nextRunAt"] is not None

            listed = await client.get(f"/api/v1/agents/{agent_id}/triggers", headers=headers)
            assert listed.status_code == 200
            assert any(t["id"] == trigger_id for t in listed.json())

            updated = await client.patch(
                f"/api/v1/triggers/{trigger_id}", json={"enabled": False}, headers=headers
            )
            assert updated.status_code == 200
            assert updated.json()["enabled"] is False

            deleted = await client.delete(f"/api/v1/triggers/{trigger_id}", headers=headers)
            assert deleted.status_code == 204

            listed_after = await client.get(f"/api/v1/agents/{agent_id}/triggers", headers=headers)
            assert all(t["id"] != trigger_id for t in listed_after.json())


async def test_create_trigger_rejects_invalid_cron(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, headers = await _agent_and_headers(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "cron", "taskText": "x", "cronExpression": "not a cron"},
                headers=headers,
            )
            assert r.status_code == 422


async def test_create_trigger_404s_for_missing_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/agents/{uuid.uuid4()}/triggers",
                json={"kind": "cron", "taskText": "x", "cronExpression": "0 9 * * *"},
                headers=headers,
            )
            assert r.status_code == 404


async def test_create_webhook_trigger_returns_a_usable_url(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, headers = await _agent_and_headers(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            create = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "webhook", "taskText": "Handle whatever this webhook sends"},
                headers=headers,
            )
            assert create.status_code == 201, create.text
            body = create.json()
            assert body["kind"] == "webhook"
            assert body["webhookUrl"] is not None
            assert "/api/v1/webhooks/" in body["webhookUrl"]
            token = body["webhookUrl"].rsplit("/", 1)[-1]
            assert len(token) >= 32  # 256-bit urlsafe token, not a short/guessable id
            # No cron/event fields leak onto a webhook row.
            assert body["cronExpression"] is None
            assert body["eventSource"] is None

            # Two webhook triggers never share a token.
            second = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "webhook", "taskText": "A second one"},
                headers=headers,
            )
            assert second.status_code == 201, second.text
            assert second.json()["webhookUrl"] != body["webhookUrl"]


async def test_cron_and_event_triggers_carry_no_webhook_url(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, headers = await _agent_and_headers(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            cron = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "cron", "taskText": "x", "cronExpression": "0 9 * * *"},
                headers=headers,
            )
            assert cron.json()["webhookUrl"] is None

            event = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={
                    "kind": "event",
                    "taskText": "x",
                    "eventSource": "github",
                    "eventType": "github.push",
                },
                headers=headers,
            )
            assert event.json()["webhookUrl"] is None


async def test_trigger_is_not_visible_to_a_different_tenant(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    other_tenant = GLOBEX_TENANT_ID
    agent_id, headers = await _agent_and_headers(app_session, tenant)
    other_token = get_identity_provider().mint(
        tenant_id=other_tenant, subject="dev-user", role="org_admin"
    )
    other_headers = {"Authorization": f"Bearer {other_token}"}

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            create = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "cron", "taskText": "x", "cronExpression": "0 9 * * *"},
                headers=headers,
            )
            trigger_id = create.json()["id"]

            listed_as_other = await client.get(
                f"/api/v1/agents/{agent_id}/triggers", headers=other_headers
            )
            assert listed_as_other.status_code == 200
            assert all(t["id"] != trigger_id for t in listed_as_other.json())

            patched_as_other = await client.patch(
                f"/api/v1/triggers/{trigger_id}", json={"enabled": False}, headers=other_headers
            )
            assert patched_as_other.status_code == 404


# --- Subscription-guard wiring (ChatGPT subscription auth design §5,
# Task 9's assert_manual_only_compatible) ------------------------------------


async def test_create_trigger_rejected_for_subscription_model_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, headers = await _subscription_agent_and_headers(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "cron", "taskText": "x", "cronExpression": "0 * * * *"},
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert "ChatGPT subscription" in r.json()["detail"]

            # And the rejected trigger must never have been persisted.
            listed = await client.get(f"/api/v1/agents/{agent_id}/triggers", headers=headers)
            assert listed.json() == []


async def test_create_webhook_trigger_also_rejected_for_subscription_model_agent(
    app_session: AppSessionFactory,
) -> None:
    """Every trigger kind counts, not just cron."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, headers = await _subscription_agent_and_headers(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "webhook", "taskText": "x"},
                headers=headers,
            )
            assert r.status_code == 422, r.text


async def test_enabling_a_trigger_rejected_for_subscription_model_agent(
    app_session: AppSessionFactory,
) -> None:
    """A trigger created while the agent had an ordinary model, then the agent
    is switched to a subscription model, must still block re-enabling it."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id, headers = await _agent_and_headers(app_session, tenant)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            create = await client.post(
                f"/api/v1/agents/{agent_id}/triggers",
                json={"kind": "cron", "taskText": "x", "cronExpression": "0 * * * *"},
                headers=headers,
            )
            assert create.status_code == 201, create.text
            trigger_id = create.json()["id"]

            disabled = await client.patch(
                f"/api/v1/triggers/{trigger_id}", json={"enabled": False}, headers=headers
            )
            assert disabled.status_code == 200, disabled.text

    async with app_session(tenant) as s:
        cred = await create_credential(
            s,
            tenant_id=tenant,
            name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        s.add(mc)
        await s.flush()
        agent = await s.get(m.Agent, agent_id)
        assert agent is not None
        agent.model_config_id = mc.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            re_enable = await client.patch(
                f"/api/v1/triggers/{trigger_id}", json={"enabled": True}, headers=headers
            )
            assert re_enable.status_code == 422, re_enable.text
            assert "ChatGPT subscription" in re_enable.json()["detail"]


async def test_disabling_a_trigger_never_blocked_by_subscription_model(
    app_session: AppSessionFactory,
) -> None:
    """Turning a trigger OFF must always succeed, even for a subscription-bound
    agent -- the guard only matters when a trigger is turning (or staying) on."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))

    async with app_session(tenant) as s:
        cred = await create_credential(
            s,
            tenant_id=tenant,
            name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        s.add(mc)
        await s.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Subscription-bound",
            model_config_id=mc.id,
        )
        s.add(agent)
        await s.flush()
        agent_id = agent.id
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="x",
            cron_expression="0 * * * *",
            enabled=True,
        )
        s.add(trigger)
        await s.flush()
        trigger_id = trigger.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            disabled = await client.patch(
                f"/api/v1/triggers/{trigger_id}", json={"enabled": False}, headers=headers
            )
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["enabled"] is False

            # A no-op update (enabled left unset) on an already-disabled row
            # must also never be newly rejected.
            re_disabled = await client.patch(
                f"/api/v1/triggers/{trigger_id}", json={"taskText": "y"}, headers=headers
            )
            assert re_disabled.status_code == 200, re_disabled.text


async def test_updating_other_fields_never_blocked_by_leaving_enabled_untouched(
    app_session: AppSessionFactory,
) -> None:
    """An update that leaves `enabled` unset must never be newly rejected, even
    when the row is (illicitly) already enabled on a subscription-bound agent
    -- the guard's job here is only to police transitions to True, not to
    re-validate every unrelated field edit against a pre-existing state."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))

    async with app_session(tenant) as s:
        cred = await create_credential(
            s,
            tenant_id=tenant,
            name=f"cg-{uuid.uuid4().hex[:8]}",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        s.add(mc)
        await s.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Subscription-bound",
            model_config_id=mc.id,
        )
        s.add(agent)
        await s.flush()
        agent_id = agent.id
        # Pre-existing enabled trigger, created directly (bypassing the API
        # guard) to simulate a row that predates this enforcement.
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="x",
            cron_expression="0 * * * *",
            enabled=True,
        )
        s.add(trigger)
        await s.flush()
        trigger_id = trigger.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            edited = await client.patch(
                f"/api/v1/triggers/{trigger_id}", json={"taskText": "y"}, headers=headers
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["taskText"] == "y"
            assert edited.json()["enabled"] is True


async def test_followup_triggers_are_invisible_to_the_trigger_api(
    app_session: AppSessionFactory,
) -> None:
    from oc8.copilot.followups import schedule_followup
    from oc8.copilot.responsibilities import open_responsibility
    from tests.copilot.helpers import copilot_seat, once_in_an_hour

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as s:
        seat = await copilot_seat(s, tenant, f"fu-{uuid.uuid4().hex[:8]}@example.com")
        session = await s.get(m.ChatSession, seat.session_id)
        assert session is not None
        r = await open_responsibility(
            s, tenant_id=tenant, member_id=seat.member_id, chat_session_id=seat.session_id,
            title="t", goal="g", notify_rule="risks_and_decisions", origin_channel=None,
            run_id=None, actor_agent_id=session.agent_id, member_subject=seat.subject,
        )  # fmt: skip
        fu = await schedule_followup(
            s, tenant_id=tenant, member_id=seat.member_id, responsibility_id=r.id,
            assistant_id=session.agent_id, spec=once_in_an_hour(), prompt="p",
            member_subject=seat.subject,
        )  # fmt: skip
        copilot_id, followup_id = session.agent_id, fu.id
    token = get_identity_provider().mint(tenant_id=tenant, subject="dev-user", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            listed = await client.get(f"/api/v1/agents/{copilot_id}/triggers", headers=headers)
            assert listed.status_code == 200
            assert all(t["id"] != str(followup_id) for t in listed.json())
            patched = await client.patch(
                f"/api/v1/triggers/{followup_id}", json={"enabled": False}, headers=headers
            )
            assert patched.status_code == 404
            deleted = await client.delete(f"/api/v1/triggers/{followup_id}", headers=headers)
            assert deleted.status_code == 404
    async with app_session(tenant) as s:
        still = await s.get(m.Trigger, followup_id)
        assert still is not None and still.enabled
