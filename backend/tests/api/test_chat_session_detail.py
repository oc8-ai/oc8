from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.chat.service import record_assistant_reply
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str = "op") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        base = m.ModelConfig(tenant_id=tenant, provider="openai", model="base", locality="cloud")
        # Switched off for chat: the picker must neither list nor accept it.
        local = m.ModelConfig(
            tenant_id=tenant,
            provider="ollama",
            model="llama",
            locality="local",
            chat_enabled=False,
        )
        db.add_all([base, local])
        await db.flush()
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A", model_config_id=base.id)
        db.add(agent)
        await db.flush()
        return agent.id, base.id, local.id


async def test_models_list_auto_first_and_reject_outside_set(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, base, local = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            models = (
                await c.get(f"/api/v1/chat/sessions/{sid}/models", headers=_headers(tenant))
            ).json()
            assert models[0] == {
                "id": None,
                "label": "Auto",
                "provider": "",
                "locality": "",
                "isDefault": False,
            }
            assert [o["id"] for o in models[1:]] == [str(base)]
            assert models[1]["isDefault"] is True

            refused = await c.put(
                f"/api/v1/chat/sessions/{sid}/model",
                json={"modelConfigId": str(local)},
                headers=_headers(tenant),
            )
            assert refused.status_code == 422, refused.text

            ok = await c.put(
                f"/api/v1/chat/sessions/{sid}/model",
                json={"modelConfigId": str(base)},
                headers=_headers(tenant),
            )
            assert ok.status_code == 200 and ok.json()["modelOverrideId"] == str(base)

            back_to_auto = await c.put(
                f"/api/v1/chat/sessions/{sid}/model",
                json={"modelConfigId": None},
                headers=_headers(tenant),
            )
            assert back_to_auto.json()["modelOverrideId"] is None


async def test_send_records_author_and_session_detail_lists_the_active_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id, base, _ = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant),
                )
            ).json()["id"]
            await c.put(
                f"/api/v1/chat/sessions/{sid}/model",
                json={"modelConfigId": str(base)},
                headers=_headers(tenant),
            )
            with patch("oc8.runtime.intake.publish_run"):
                sent = await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    json={"message": "hello"},
                    headers=_headers(tenant),
                )
            assert sent.status_code == 201, sent.text
            body = sent.json()
            assert body["authorMemberId"] is not None
            assert "authorName" in body
            detail = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=_headers(tenant))).json()
            assert detail["modelOverrideId"] == str(base)
            assert detail["kind"] == "chat" and detail["readOnly"] is False
            assert detail["activeRuns"][0]["runId"] == body["runId"]
            assert detail["activeRuns"][0]["replyToId"] == body["id"]
    async with app_session(tenant) as db:
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id))
        ).scalar_one()
        assert run.context["model_override_id"] == str(base)
        assert run.context["reply_to_message_id"] == body["id"]

        await record_assistant_reply(db, run=run, output="hi")
        await db.flush()
        reply = (
            await db.execute(select(m.ChatMessage).where(m.ChatMessage.run_id == run.id))
        ).scalar_one()
        assert str(reply.reply_to_id) == body["id"]


async def test_a_new_chat_lists_its_agents_models_without_a_session(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, base, _ = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/chat/agents/{agent_id}/models", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            models = r.json()
            assert models[0]["id"] is None and models[0]["label"] == "Auto"
            assert [o["id"] for o in models[1:]] == [str(base)]
            assert models[1]["isDefault"] is True
            # Listing creates no chat.
            sessions = await c.get("/api/v1/chat/sessions", headers=_headers(tenant))
            assert sessions.json() == []


async def test_models_of_an_unreachable_agent_are_404(app_session: AppSessionFactory) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    foreign_agent, _, _ = await _seed(app_session, other)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            for agent_id in (foreign_agent, uuid.uuid4()):
                r = await c.get(f"/api/v1/chat/agents/{agent_id}/models", headers=_headers(tenant))
                assert r.status_code == 404, r.text


async def test_another_members_session_detail_is_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, _, _ = await _seed(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = (
                await c.post(
                    "/api/v1/chat/sessions",
                    json={"agentId": str(agent_id)},
                    headers=_headers(tenant, "alice"),
                )
            ).json()["id"]
            r = await c.get(f"/api/v1/chat/sessions/{sid}", headers=_headers(tenant, "bob"))
            assert r.status_code == 404
