"""Editing a sent message and switching between branches (spec §5.3): an edit
is a new user turn beside the edited one, the old branch stays, and a switch
moves the person to the newest message below the one they picked."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.auth import get_identity_provider
from oc8.chat.service import edit_message, record_answer_turn, record_assistant_reply
from oc8.chat.turn_context import flattened_task_text
from oc8.main import create_app
from tests.chat.test_message_tree import (
    _commit,
    _leaf,
    _parent,
    _reply,
    _send,
    _session,
    audit_lock_comes_first,
    statements,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _edit(
    db: Any, session: m.ChatSession, original: m.ChatMessage, content: str
) -> tuple[m.ChatMessage, m.AgentRun]:
    member = await db.get(m.OrgMember, session.member_id)
    msg, run = await edit_message(
        db,
        session=session,
        tenant_id=session.tenant_id,
        original=original,
        content=content,
        originating_operator="op",
        operator_role=None,
        acting_member=member,
    )
    assert run is not None
    return msg, run


async def test_edit_creates_a_sibling_and_keeps_the_old_branch(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        u2, run2 = await _send(db, session, "/plan two")
        r2 = await _reply(db, run2, "answer two")
        edited, _run = await _edit(db, session, u2, "two, but better")
        assert edited.id != u2.id
        assert edited.role == "user" and edited.content == "two, but better"
        assert await _parent(db, edited.id) == await _parent(db, u2.id) == r1.id
        assert await _leaf(db, session) == edited.id
        assert edited.mode == u2.mode == "plan", "the turn's mode carries over"
        assert edited.author_member_id == session.member_id
        kept = await db.execute(
            select(m.ChatMessage.id).where(m.ChatMessage.id.in_([u2.id, r2.id]))
        )
        assert set(kept.scalars()) == {u2.id, r2.id}, "the old branch is never deleted"


async def test_edited_turn_and_the_next_one_follow_the_new_branch(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        await _reply(db, run1, "ANSWER-ONE")
        u2, run2 = await _send(db, session, "ORIGINAL-TWO")
        await _reply(db, run2, "ANSWER-TWO")
        _edited, edit_run = await _edit(db, session, u2, "EDITED-TWO")
        await _reply(db, edit_run, "ANSWER-EDITED")
        _u3, run3 = await _send(db, session, "three")
        edit_task = await flattened_task_text(db, edit_run)
        task = await flattened_task_text(db, run3)
    assert "ANSWER-ONE" in edit_task and "EDITED-TWO" in edit_task
    assert "ORIGINAL-TWO" not in edit_task and "ANSWER-TWO" not in edit_task
    assert "EDITED-TWO" in task and "ANSWER-EDITED" in task
    assert "ORIGINAL-TWO" not in task and "ANSWER-TWO" not in task


async def test_editing_a_parked_answer_branches_beside_it(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        u, run = await _send(db, session, "book a room")
        answer = await record_answer_turn(
            db,
            run=run,
            question="Which floor?",
            pending=None,
            structured=None,
            text="the third",
            author_member_id=session.member_id,
        )
        assert answer is not None
        await _commit(db, tenant)
        edited, _run = await _edit(db, session, answer, "the fourth")
        assert await _parent(db, edited.id) == u.id
        assert await _leaf(db, session) == edited.id


# --- the doors -------------------------------------------------------------


def _token(tenant: uuid.UUID, subject: str = "op") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _seed_agent(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        return agent.id


@asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            with patch("oc8.runtime.intake.publish_run"):
                yield c


async def _new_chat(c: AsyncClient, tenant: uuid.UUID, agent_id: uuid.UUID) -> str:
    r = await c.post(
        "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=_token(tenant)
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _post(c: AsyncClient, tenant: uuid.UUID, sid: str, text: str, **body: Any) -> Any:
    r = await c.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"message": text, **body},
        headers=_token(tenant),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _answer(app_session: AppSessionFactory, tenant: uuid.UUID, run_id: str, text: str) -> str:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        run.state = "done"
        await record_assistant_reply(db, run=run, output=text)
        reply = (
            await db.execute(
                select(m.ChatMessage.id).where(
                    m.ChatMessage.run_id == run.id, m.ChatMessage.role == "assistant"
                )
            )
        ).scalar_one()
        return str(reply)


async def _task(app_session: AppSessionFactory, tenant: uuid.UUID, run_id: str) -> str:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        return await flattened_task_text(db, run)


async def test_edit_door_answers_with_the_new_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        u1 = await _post(c, tenant, sid, "one")
        r1 = await _answer(app_session, tenant, u1["runId"], "answer one")
        with statements() as seen:
            r = await c.post(
                f"/api/v1/chat/sessions/{sid}/messages/{u1['id']}/edit",
                json={"content": "one, edited"},
                headers=_token(tenant),
            )
        assert r.status_code == 201, r.text
        edited = r.json()
        assert edited["content"] == "one, edited" and edited["role"] == "user"
        assert edited["parentId"] is None and edited["runId"]
        session = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=_token(tenant))).json()
        assert session["currentLeafId"] == edited["id"]
        # An assistant's reply is not a person's words: nothing to edit.
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{r1}/edit",
            json={"content": "rewritten"},
            headers=_token(tenant),
        )
        assert r.status_code == 409, r.text
    audit_lock_comes_first(seen, tenant)


async def test_edit_door_refuses_a_follow_up(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        async with app_session(tenant) as db:
            followup = m.ChatMessage(
                tenant_id=tenant, session_id=uuid.UUID(sid), role="followup", content="ping"
            )
            db.add(followup)
            await db.flush()
            followup_id = followup.id
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{followup_id}/edit",
            json={"content": "pong"},
            headers=_token(tenant),
        )
        assert r.status_code == 409, r.text


async def test_only_the_author_may_edit(app_session: AppSessionFactory, redis_url: str) -> None:
    """An overseer reaches a colleague's Copilot chat (`copilot:manage`), but
    may not put other words in the colleague's mouth."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        assistant_id = assistant.id
    async with _client() as c:
        sid = await _new_chat(c, tenant, assistant_id)
        u1 = await _post(c, tenant, sid, "draft my week")
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{u1['id']}/edit",
            json={"content": "something else"},
            headers=_token(tenant, subject="overseer"),
        )
        assert r.status_code == 403, r.text


async def test_edit_and_switch_in_a_foreign_session_are_not_found(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        u1 = await _post(c, tenant, sid, "mine")
        other = _token(tenant, subject="someone-else")
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{u1['id']}/edit",
            json={"content": "theirs"},
            headers=other,
        )
        assert r.status_code == 404, r.text
        r = await c.put(
            f"/api/v1/chat/sessions/{sid}/leaf", json={"messageId": u1["id"]}, headers=other
        )
        assert r.status_code == 404, r.text
        # A message of another chat, named in this one.
        other_sid = await _new_chat(c, tenant, agent_id)
        elsewhere = await _post(c, tenant, other_sid, "elsewhere")
        r = await c.put(
            f"/api/v1/chat/sessions/{sid}/leaf",
            json={"messageId": elsewhere["id"]},
            headers=_token(tenant),
        )
        assert r.status_code == 404, r.text
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{elsewhere['id']}/edit",
            json={"content": "x"},
            headers=_token(tenant),
        )
        assert r.status_code == 404, r.text


async def test_edit_and_switch_need_a_tree(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        u1 = await _post(c, tenant, sid, "one")
        async with app_session(tenant) as db:
            row = await db.get(m.ChatSession, uuid.UUID(sid))
            assert row is not None
            row.kind = "live"
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{u1['id']}/edit",
            json={"content": "x"},
            headers=_token(tenant),
        )
        assert r.status_code == 409, r.text
        r = await c.put(
            f"/api/v1/chat/sessions/{sid}/leaf",
            json={"messageId": u1["id"]},
            headers=_token(tenant),
        )
        assert r.status_code == 409, r.text


async def test_attachments_carry_over_to_the_edited_turn(
    app_session: AppSessionFactory, redis_url: str, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        upload = await c.post(
            f"/api/v1/chat/sessions/{sid}/attachments",
            files={"file": ("notes.txt", b"the quarterly numbers", "text/plain")},
            headers=_token(tenant),
        )
        assert upload.status_code == 201, upload.text
        attachment_id = upload.json()["id"]
        u1 = await _post(c, tenant, sid, "see attached", attachmentIds=[attachment_id])
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{u1['id']}/edit",
            json={"content": "see attached, please"},
            headers=_token(tenant),
        )
        assert r.status_code == 201, r.text
        edited = r.json()
        [copy] = edited["attachments"]
        assert copy["id"] != attachment_id and copy["filename"] == "notes.txt"
        download = await c.get(f"/api/v1/files/{copy['id']}", headers=_token(tenant))
        assert download.status_code == 200, download.text
        assert download.content == b"the quarterly numbers"
    task = await _task(app_session, tenant, edited["runId"])
    assert "[Attached file: notes.txt]" in task and "the quarterly numbers" in task
    async with app_session(tenant) as db:
        original = await db.get(m.FileAttachment, uuid.UUID(attachment_id))
        assert original is not None and str(original.owner_id) == u1["id"], (
            "the old branch keeps its file"
        )


async def test_switch_resolves_to_the_newest_leaf_and_steers_the_next_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        u1 = await _post(c, tenant, sid, "one")
        await _answer(app_session, tenant, u1["runId"], "ANSWER-ONE")
        u2 = await _post(c, tenant, sid, "ORIGINAL-TWO")
        r2 = await _answer(app_session, tenant, u2["runId"], "ANSWER-TWO")
        edited = (
            await c.post(
                f"/api/v1/chat/sessions/{sid}/messages/{u2['id']}/edit",
                json={"content": "EDITED-TWO"},
                headers=_token(tenant),
            )
        ).json()
        await _answer(app_session, tenant, edited["runId"], "ANSWER-EDITED")
        # Back to the original branch, picked at its (inner) user turn.
        with (
            statements() as seen,
            patch("oc8.chat.tree.publish_chat_session_updated") as announced,
        ):
            r = await c.put(
                f"/api/v1/chat/sessions/{sid}/leaf",
                json={"messageId": u2["id"]},
                headers=_token(tenant),
            )
        assert r.status_code == 200, r.text
        assert r.json()["currentLeafId"] == r2
        assert announced.called, "the switch is announced as chat.session_updated"
        u3 = await _post(c, tenant, sid, "three")
        assert u3["parentId"] == r2
    audit_lock_comes_first(seen, tenant)
    task = await _task(app_session, tenant, u3["runId"])
    assert "ORIGINAL-TWO" in task and "ANSWER-TWO" in task
    assert "EDITED-TWO" not in task and "ANSWER-EDITED" not in task
