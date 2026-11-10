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


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _agent(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        return agent.id


async def _finish(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: str, text: str
) -> None:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        run.state = "done"
        await record_assistant_reply(db, run=run, output=text)


async def test_regenerate_keeps_both_answers_on_the_same_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
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
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"message": "draft a reply"},
                        headers=_headers(tenant),
                    )
                ).json()
            await _finish(app_session, tenant, turn["runId"], "first answer")
            with patch("oc8.runtime.intake.publish_run"):
                again = await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages/{turn['id']}/regenerate",
                    headers=_headers(tenant),
                )
            assert again.status_code == 201, again.text
            assert again.json()["replyToId"] == turn["id"]
            await _finish(app_session, tenant, again.json()["runId"], "second answer")
            msgs = (
                await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=_headers(tenant))
            ).json()
    answers = [x for x in msgs if x["role"] == "assistant"]
    assert [a["content"] for a in answers] == ["first answer", "second answer"]
    assert {a["replyToId"] for a in answers} == {turn["id"]}
    assert len([x for x in msgs if x["role"] == "user"]) == 1


async def test_regenerate_refuses_while_an_answer_is_running(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
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
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"message": "x"},
                        headers=_headers(tenant),
                    )
                ).json()
                r = await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages/{turn['id']}/regenerate",
                    headers=_headers(tenant),
                )
            assert r.status_code == 409


async def test_feedback_is_audited(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
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
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"message": "x"},
                        headers=_headers(tenant),
                    )
                ).json()
            await _finish(app_session, tenant, turn["runId"], "answer")
            msgs = (
                await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=_headers(tenant))
            ).json()
            answer_id = next(x["id"] for x in msgs if x["role"] == "assistant")
            r = await c.post(
                f"/api/v1/chat/sessions/{sid}/messages/{answer_id}/feedback",
                json={"rating": "down", "note": "wrong number"},
                headers=_headers(tenant),
            )
            assert r.status_code == 204
    async with app_session(tenant) as db:
        rows = (
            (await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == "chat.feedback")))
            .scalars()
            .all()
        )
    assert rows and rows[0].resource["rating"] == "down"


async def _assistant_session(c: AsyncClient, tenant: uuid.UUID) -> str:
    agent_id = (await c.get("/api/v1/assistant", headers=_headers(tenant))).json()["agentId"]
    r = await c.post("/api/v1/chat/sessions", json={"agentId": agent_id}, headers=_headers(tenant))
    return str(r.json()["id"])


async def test_an_overseer_cannot_regenerate_or_rate_in_a_members_session(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    boss = {
        "Authorization": "Bearer "
        + get_identity_provider().mint(tenant_id=tenant, subject="boss", role="org_admin")
    }
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sid = await _assistant_session(c, tenant)
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"message": "hi"},
                        headers=_headers(tenant),
                    )
                ).json()
            await _finish(app_session, tenant, turn["runId"], "answer")
            # The oversight carve-out still reads the session ...
            assert (
                await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=boss)
            ).status_code == 200
            msgs = (
                await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=_headers(tenant))
            ).json()
            answer_id = next(x["id"] for x in msgs if x["role"] == "assistant")
            base = f"/api/v1/chat/sessions/{sid}/messages"
            r1 = await c.post(f"{base}/{turn['id']}/regenerate", headers=boss)
            r2 = await c.post(f"{base}/{answer_id}/feedback", json={"rating": "up"}, headers=boss)
            assert (r1.status_code, r2.status_code) == (404, 404)
            ok = await c.post(
                f"{base}/{answer_id}/feedback", json={"rating": "up"}, headers=_headers(tenant)
            )
            assert ok.status_code == 204


async def test_regenerate_and_feedback_guard_their_targets(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _headers(tenant)
            sids = []
            turns = []
            for _ in range(2):
                sid = (
                    await c.post(
                        "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h
                    )
                ).json()["id"]
                with patch("oc8.runtime.intake.publish_run"):
                    turn = (
                        await c.post(
                            f"/api/v1/chat/sessions/{sid}/messages",
                            json={"message": "x"},
                            headers=h,
                        )
                    ).json()
                await _finish(app_session, tenant, turn["runId"], "answer")
                sids.append(sid)
                turns.append(turn)
            base = f"/api/v1/chat/sessions/{sids[0]}/messages"
            msgs = (await c.get(base, headers=h)).json()
            answer_id = next(x["id"] for x in msgs if x["role"] == "assistant")
            assert (await c.post(f"{base}/{answer_id}/regenerate", headers=h)).status_code == 409
            fb = await c.post(f"{base}/{turns[0]['id']}/feedback", json={"rating": "up"}, headers=h)
            assert fb.status_code == 409
            other = turns[1]["id"]
            assert (await c.post(f"{base}/{other}/regenerate", headers=h)).status_code == 404
            fb2 = await c.post(f"{base}/{other}/feedback", json={"rating": "up"}, headers=h)
            assert fb2.status_code == 404


async def test_regenerate_run_context_carries_the_turn_and_its_mode(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Policies", embedding_model="e")
        db.add(kb)
        await db.flush()
        kb_id = str(kb.id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _headers(tenant)
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
            ).json()["id"]
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={
                            "message": "/ask what is policy",
                            "contextRefs": [{"kind": "knowledge_base", "id": kb_id}],
                        },
                        headers=h,
                    )
                ).json()
            await _finish(app_session, tenant, turn["runId"], "a")
            with patch("oc8.runtime.intake.publish_run"):
                again = await c.post(
                    f"/api/v1/chat/sessions/{sid}/messages/{turn['id']}/regenerate", headers=h
                )
            assert again.status_code == 201, again.text
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(again.json()["runId"]))
    assert run is not None
    ctx = run.context
    assert ctx["reply_to_message_id"] == turn["id"]
    assert ctx["chat_mode"] == "ask"
    assert ctx["context_kb_ids"] == [kb_id]
