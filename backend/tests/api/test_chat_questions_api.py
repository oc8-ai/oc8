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
            # Answered beats empty: the state is reported, not the body.
            empty_again = await c.post(url, json={}, headers=alice)
            assert empty_again.status_code == 409

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


async def _session_with_parked_question(
    c: AsyncClient,
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    agent_id: uuid.UUID,
    headers: dict[str, str],
    *,
    structured: bool = True,
) -> tuple[str, str, uuid.UUID]:
    sid = (
        await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=headers)
    ).json()["id"]
    with patch("oc8.runtime.intake.publish_run"):
        turn = (
            await c.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"message": "go"}, headers=headers
            )
        ).json()
    clar_id = await _park(app_session, tenant, turn["runId"], structured=structured)
    return sid, turn["runId"], clar_id


async def test_answers_are_bounded(app_session: AppSessionFactory, redis_url: str) -> None:
    """Every dimension of the request is capped, and anything outside the
    questions asked is refused -- never stored, never resumed."""
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            alice = _h(tenant, "alice")
            sid, run_id, clar_id = await _session_with_parked_question(
                c, app_session, tenant, agent_id, alice
            )
            url = f"/api/v1/chat/sessions/{sid}/questions/{clar_id}/answer"
            refused = [
                {"answers": {f"q{i}": "x" for i in range(7)}},  # > 6 entries
                {"answers": {"r": ["eu"], "x" * 65: "x"}},  # id > 64 chars
                {"answers": {"r": ["eu"] * 9}},  # > 8 keys per answer
                {"answers": {"r": ["e" * 65]}},  # key > 64 chars
                {"answers": {"r": ["eu"], "n": "x" * 2001}},  # free text > 2000
                {"text": "x" * 2001},  # written answer > 2000
                {"answers": {"r": ["eu"]}, "extra": 1},  # unknown field
                {"answers": {"r": ["eu"], "zzz": "x"}},  # unknown question id
                {"answers": {"r": ["mars"]}},  # key not among the options
            ]
            with patch("oc8.api.v1.chat.publish_run") as published:
                for body in refused:
                    got = await c.post(url, json=body, headers=alice)
                    assert got.status_code == 422, (body, got.text)
            assert not published.called

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None and run.state == "waiting_for_input"
        clar = await db.get(m.Clarification, clar_id)
        assert clar is not None and clar.status == "open"


async def test_a_secret_in_an_answer_to_the_assistant_is_refused_like_a_message(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The Assistant's secret-blindness gate (`send_message`) applies to an
    answer too: the turn is kept redacted, the refusal is said, and the
    credential never reaches the run -- which stays parked."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            alice = _h(tenant, "alice")
            assistant_id = uuid.UUID(
                (await c.get("/api/v1/assistant", headers=alice)).json()["agentId"]
            )
            sid, run_id, clar_id = await _session_with_parked_question(
                c, app_session, tenant, assistant_id, alice
            )
            url = f"/api/v1/chat/sessions/{sid}/questions/{clar_id}/answer"
            with patch("oc8.api.v1.chat.publish_run") as published:
                got = await c.post(
                    url,
                    json={"answers": {"r": ["eu"], "n": "password=hunter2"}},
                    headers=alice,
                )
            assert got.status_code == 422, got.text
            assert "credential" in got.json()["detail"]
            assert not published.called
            messages = (await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=alice)).json()
            assert "hunter2" not in str(messages)
            # Same transaction, same millisecond: uuid7 order between the two is
            # not fixed, so find them by role rather than by position.
            turns = [(x["role"], x["content"]) for x in messages[1:]]
            assert len(turns) == 2
            [user_turn] = [c for r, c in turns if r == "user"]
            [refusal] = [c for r, c in turns if r == "assistant"]
            assert "[redacted]" in user_turn
            assert "credential" in refusal

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None and run.state == "waiting_for_input"
        assert "hunter2" not in str(run.context)
        clar = await db.get(m.Clarification, clar_id)
        assert clar is not None and clar.status == "open" and clar.answer is None


async def test_the_inbox_and_admin_doors_also_write_the_answered_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Spec §4.1: whichever door answers a chat run's question, its chat keeps
    the answered card -- not only the chat's own door."""
    tenant = uuid.uuid4()
    agent_id = await _agent(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            alice = _h(tenant, "alice")
            sid, _run, clar_id = await _session_with_parked_question(
                c, app_session, tenant, agent_id, alice
            )
            with patch("oc8.api.v1.clarifications.publish_run"):
                inbox = await c.post(
                    f"/api/v1/clarifications/{clar_id}/answer",
                    json={"answer": "1: 2"},
                    headers=alice,
                )
            assert inbox.status_code == 200, inbox.text

            sid2, run2, _clar2 = await _session_with_parked_question(
                c, app_session, tenant, agent_id, alice, structured=False
            )
            with patch("oc8.api.v1.run.publish_run"):
                admin = await c.post(
                    f"/api/v1/runs/{run2}/answer", json={"answer": "4711"}, headers=alice
                )
            assert admin.status_code == 200, admin.text

            first = (await c.get(f"/api/v1/chat/sessions/{sid}/messages", headers=alice)).json()
            second = (await c.get(f"/api/v1/chat/sessions/{sid2}/messages", headers=alice)).json()

    assert first[-1]["role"] == "user"
    assert first[-1]["content"] == "Region?: US"
    assert first[-1]["parts"][0]["answers"] == {"r": ["us"]}
    assert "[step" not in first[-1]["content"]
    assert second[-1]["content"] == "Order number?: 4711"
    assert second[-1]["parts"][0]["answers"] == {"q1": "4711"}
