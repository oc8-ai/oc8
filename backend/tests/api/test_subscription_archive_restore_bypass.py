"""The archive -> rebind -> restore bypass of the manual-trigger-only rule
(final whole-branch re-review, "Ruling B").

`oc8.modelrouter.subscription_guard` enforces one property: a ChatGPT-
subscription-backed model may never be reachable from an agent that has an
enabled trigger, because a personal subscription is not licensed for
unattended runs. Every write path that could create that pairing is guarded.

The hole this file locks shut: an ARCHIVED agent used to be invisible to the
model-side half of the guard (`assert_credential_bind_safe` filtered on
`Agent.deleted_at IS NULL`), while the agent-side half never excluded archived
agents at all -- so the two directions disagreed, and archiving was a way to
step out from under one of them:

1. an agent with an ordinary API-key model and an enabled cron trigger,
2. `DELETE /agents/{id}` -> archived (needs `agent:manage`),
3. `PATCH /models/{id}` binding a subscription credential -- used to pass,
   because the only agent reaching that model was soft-deleted,
4. `POST /agents/{id}/restore` (needs `agent:manage`) -- re-checked nothing,
5. a live agent with an enabled cron trigger running on a personal ChatGPT
   subscription: exactly what the feature exists to prevent.

Both halves of the fix are covered here: step 3 is rejected while an archived
agent still has an enabled trigger (the front door, consistent with how the
agent-side direction has always treated archived agents), and step 4 re-checks
the agent it is bringing back to life, so state that reached the database some
other way cannot be revived either.

Since archiving also disables the agent's triggers (`oc8.agents.retire`), the
HTTP path closes earlier still: after step 2 nothing unattended reaches the
model, so the bind in step 3 is safe, restoring leaves the trigger off, and
turning it back on is what the guard refuses.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import config
from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="boss", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_credential` envelope-encrypts, so a KEK must exist."""
    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


async def test_archive_then_rebind_then_restore_cannot_smuggle_a_subscription_model(
    app_session: AppSessionFactory,
) -> None:
    """The whole exploit, end to end over HTTP, and the state it must not reach."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sub = await create_credential(
            db,
            tenant_id=tenant,
            name="My ChatGPT",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        api_key = await create_credential(
            db,
            tenant_id=tenant,
            name="Work OpenAI",
            credential_type="openai_api_key",
            field_values={"api_key": "sk-x"},
        )
        mc = m.ModelConfig(
            tenant_id=tenant,
            provider="openai_chatgpt",
            model="gpt-5",
            credential_id=api_key.id,
        )
        db.add(mc)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Scheduled",
            model_config_id=mc.id,
        )
        db.add(agent)
        await db.flush()
        # A run makes DELETE archive the agent instead of hard-deleting it.
        db.add(m.AgentRun(tenant_id=tenant, agent_id=agent.id, context={}))
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="daily",
            cron_expression="0 * * * *",
            enabled=True,
        )
        db.add(trigger)
        await db.flush()
        agent_id, model_id, sub_id, trigger_id = agent.id, mc.id, sub.id, trigger.id

    async with _http() as http:
        headers = _headers(tenant)

        archived = await http.delete(f"/api/v1/agents/{agent_id}", headers=headers)
        assert archived.status_code == 200, archived.text
        assert archived.json()["outcome"] == "archived"

        # Step 3: archiving turned the trigger off, so nothing unattended
        # reaches the model any more and the bind is safe.
        rebind = await http.patch(
            f"/api/v1/models/{model_id}",
            json={
                "provider": "openai_chatgpt",
                "model": "gpt-5",
                "credentialId": str(sub_id),
            },
            headers=headers,
        )
        assert rebind.status_code == 200, rebind.text

        # Step 4 works and brings the agent back without its trigger.
        restored = await http.post(f"/api/v1/agents/{agent_id}/restore", headers=headers)
        assert restored.status_code == 200, restored.text

        # Turning the trigger back on is where the guard now refuses.
        reenable = await http.patch(
            f"/api/v1/triggers/{trigger_id}", json={"enabled": True}, headers=headers
        )
        assert reenable.status_code == 422, reenable.text
        assert "ChatGPT subscription" in reenable.json()["detail"]

    async with app_session(tenant) as db:
        restored_agent = await db.get(m.Agent, agent_id)
        assert restored_agent is not None
        assert restored_agent.deleted_at is None
        # The forbidden end state: live agent + enabled trigger + subscription model.
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.enabled is False


async def test_restore_rejects_an_agent_that_would_run_unattended_on_a_subscription(
    app_session: AppSessionFactory,
) -> None:
    """Defence in depth for the same property, at the moment it would start to
    matter: restore is what makes an unattended agent live again, so it
    re-checks the agent it is reviving rather than trusting that no write path
    ever produced this pairing (here it is written straight to the database,
    standing in for any route that is not the guarded ones)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sub = await create_credential(
            db,
            tenant_id=tenant,
            name="My ChatGPT",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=sub.id
        )
        db.add(mc)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Archived scheduled",
            model_config_id=mc.id,
            deleted_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add(agent)
        await db.flush()
        db.add(
            m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron",
                task_text="daily",
                cron_expression="0 * * * *",
                enabled=True,
            )
        )
        await db.flush()
        agent_id = agent.id

    async with _http() as http:
        resp = await http.post(f"/api/v1/agents/{agent_id}/restore", headers=_headers(tenant))
        assert resp.status_code == 422, resp.text
        assert "ChatGPT subscription" in resp.json()["detail"]

    async with app_session(tenant) as db:
        still_archived = await db.get(m.Agent, agent_id)
        assert still_archived is not None
        assert still_archived.deleted_at is not None


async def test_restore_allows_a_subscription_agent_with_no_enabled_trigger(
    app_session: AppSessionFactory,
) -> None:
    """The guard must not turn "archived + subscription model" into a
    permanently unrestorable agent: only the trigger makes it unattended."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sub = await create_credential(
            db,
            tenant_id=tenant,
            name="My ChatGPT",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=sub.id
        )
        db.add(mc)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Archived manual",
            model_config_id=mc.id,
            deleted_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add(agent)
        await db.flush()
        db.add(
            m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron",
                task_text="daily",
                cron_expression="0 * * * *",
                enabled=False,
            )
        )
        await db.flush()
        agent_id = agent.id

    async with _http() as http:
        resp = await http.post(f"/api/v1/agents/{agent_id}/restore", headers=_headers(tenant))
        assert resp.status_code == 200, resp.text


async def test_rebind_is_refused_while_an_archived_agent_still_has_an_enabled_trigger(
    app_session: AppSessionFactory,
) -> None:
    """Step 3's front door, for an archived agent whose trigger is still on --
    data archived before archiving turned triggers off, or written some other
    way. The model-side half of the guard still counts it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sub = await create_credential(
            db,
            tenant_id=tenant,
            name="My ChatGPT",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        api_key = await create_credential(
            db,
            tenant_id=tenant,
            name="Work OpenAI",
            credential_type="openai_api_key",
            field_values={"api_key": "sk-x"},
        )
        mc = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=api_key.id
        )
        db.add(mc)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Legacy archived",
            model_config_id=mc.id,
            deleted_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add(agent)
        await db.flush()
        db.add(
            m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron",
                task_text="daily",
                cron_expression="0 * * * *",
                enabled=True,
            )
        )
        await db.flush()
        model_id, sub_id = mc.id, sub.id

    async with _http() as http:
        rebind = await http.patch(
            f"/api/v1/models/{model_id}",
            json={"provider": "openai_chatgpt", "model": "gpt-5", "credentialId": str(sub_id)},
            headers=_headers(tenant),
        )
        assert rebind.status_code == 422, rebind.text
        assert "ChatGPT subscription" in rebind.json()["detail"]
