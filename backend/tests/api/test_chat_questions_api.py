"""The chat's own door to a question its run parked on (spec §4.1)."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.chat.questions import parse_questions, render_questions_text
from oc8.main import create_app
from oc8.runtime.clarification import request_clarification
from tests.conftest import AppSessionFactory


def _h(tenant: uuid.UUID, subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


_QUESTIONS = parse_questions(
    {
        "questions": [
            {
                "id": "r",
                "prompt": "Region?",
                "kind": "single",
                "options": [{"key": "eu", "label": "EU"}, {"key": "us", "label": "US"}],
            },
            {"id": "n", "prompt": "Notes?", "kind": "text", "required": False},
        ]
    }
)


async def _agent(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        return agent.id


async def _park(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: str, *, structured: bool = True
) -> uuid.UUID:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        run.state = "running"
        await db.flush()
        if structured:
            clar = await request_clarification(
                db, run=run, question=render_questions_text(_QUESTIONS), questions=_QUESTIONS
            )
        else:
            clar = await request_clarification(db, run=run, question="Order number?")
        return clar.id


async def test_session_shows_the_question_and_only_its_owner_answers(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            alice = _h(tenant, "alice")
            sid = (
                await c.post(
                    "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=alice
                )
            ).json()["id"]
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"message": "go"},
                        headers=alice,
                    )
                ).json()
            clar_id = await _park(app_session, tenant, turn["runId"])

            detail = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=alice)).json()
            pending = detail["pendingQuestion"]
            assert pending["clarificationId"] == str(clar_id)
            assert pending["runId"] == turn["runId"]
            assert pending["text"].startswith("Region?")
            assert [q["id"] for q in pending["questions"]] == ["r", "n"]
            assert pending["questions"][0]["options"][1] == {
                "key": "us",
                "label": "US",
                "detail": "",
            }
            assert pending["questions"][1]["allowOther"] is False
            assert detail["taskId"]

            url = f"/api/v1/chat/sessions/{sid}/questions/{clar_id}/answer"
            other = await c.post(url, json={"answers": {"r": ["eu"]}}, headers=_h(tenant, "bob"))
            assert other.status_code == 404
            bad = await c.post(url, json={"answers": {"r": ["mars"]}}, headers=alice)
            assert bad.status_code == 422
            empty = await c.post(url, json={}, headers=alice)
            assert empty.status_code == 422
            with patch("oc8.api.v1.chat.publish_run") as published:
                ok = await c.post(url, json={"answers": {"r": ["eu"], "n": "asap"}}, headers=alice)
            assert ok.status_code == 200, ok.text
            assert ok.json() == {"runId": turn["runId"]}
            assert published.called
            again = await c.post(url, json={"text": "eu"}, headers=alice)
            assert again.status_code == 409

            detail = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=alice)).json()
            assert detail["pendingQuestion"] is None

            # The answered summary survives a reload as a user turn with parts.
            messages = (await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=alice)).json()
            answered = messages[-1]
            assert answered["role"] == "user"
            assert answered["content"] == "Region?: EU\nNotes?: asap"
            [part] = answered["parts"]
            assert part["type"] == "answers"
            assert [q["id"] for q in part["questions"]] == ["r", "n"]
            assert part["answers"] == {"r": ["eu"], "n": "asap"}

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(turn["runId"]))
        assert run is not None and run.state == "queued"
        assert run.context["clarifications"][-1]["answers"] == {"r": ["eu"], "n": "asap"}
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "clarification.answered")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].resource["via"] == "chat"
        assert events[0].resource["session_id"] == sid


async def test_a_plain_question_is_answered_with_text(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            alice = _h(tenant, "alice")
            sid = (
                await c.post(
                    "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=alice
                )
            ).json()["id"]
            with patch("oc8.runtime.intake.publish_run"):
                turn = (
                    await c.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"message": "go"},
                        headers=alice,
                    )
                ).json()
            clar_id = await _park(app_session, tenant, turn["runId"], structured=False)

            detail = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=alice)).json()
            assert detail["pendingQuestion"]["text"] == "Order number?"
            assert detail["pendingQuestion"]["questions"] == []

            with patch("oc8.api.v1.chat.publish_run"):
                ok = await c.post(
                    f"/api/v1/chat/sessions/{sid}/questions/{clar_id}/answer",
                    json={"text": "4711"},
                    headers=alice,
                )
            assert ok.status_code == 200, ok.text
            messages = (await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=alice)).json()
            assert messages[-1]["content"] == "Order number?: 4711"
            assert messages[-1]["parts"][0]["answers"] == {"q1": "4711"}

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(turn["runId"]))
        assert run is not None
        assert run.context["clarifications"][-1]["answer"] == "4711"
