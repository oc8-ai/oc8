"""A personal agent's answer feedback keeps its note from everyone but the owner.

The `chat.feedback` event is written by the person who rated, so its actor is
an operator, not the agent: the audit read could not tell it was about a
personal agent, and `note` was no content key, so an `audit:view` admin read
what the owner wrote about their own agent's answer. The event now names its
agent and department, and `note` is content. Events written before carry no
agent: their note is shown to the person who wrote it and nobody else.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event
from tests.api.test_personal_agent_content import _headers, _http, _subject_uuid, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_NOTE = "It mixed up my landlord's letters again."


async def _chat_with_answer(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent: uuid.UUID, subject: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """A chat of `subject` with `agent`, holding one answer."""
    async with app_session(tenant) as db:
        member_id = await _member_id(db, subject)
        session = m.ChatSession(tenant_id=tenant, agent_id=agent, member_id=member_id)
        db.add(session)
        await db.flush()
        answer = m.ChatMessage(
            tenant_id=tenant, session_id=session.id, role="assistant", content="Done."
        )
        db.add(answer)
        await db.flush()
        return session.id, answer.id


async def _member_id(db: AsyncSession, subject: str) -> uuid.UUID:
    return (
        await db.execute(
            select(m.OrgMember.id).where(m.OrgMember.subject_uuid == _subject_uuid(subject))
        )
    ).scalar_one()


async def _rate(
    http: AsyncClient,
    tenant: uuid.UUID,
    chat: tuple[uuid.UUID, uuid.UUID],
    subject: str,
    role: str,
    rating: str = "down",
) -> None:
    session_id, message_id = chat
    r = await http.post(
        f"/api/v1/chat/sessions/{session_id}/messages/{message_id}/feedback",
        json={"rating": rating, "note": _NOTE},
        headers=_headers(tenant, subject, role),
    )
    assert r.status_code == 204, r.text


async def _feedback_events(
    http: AsyncClient, tenant: uuid.UUID, subject: str
) -> list[dict[str, object]]:
    r = await http.get(
        "/api/v1/audit?action=chat.feedback", headers=_headers(tenant, subject, "org_admin")
    )
    assert r.status_code == 200, r.text
    return [e["resource"] for e in r.json()["events"]]


async def test_an_admin_sees_a_personal_agents_feedback_without_its_note(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    chat = await _chat_with_answer(app_session, w.tenant, w.personal_agent, "owner")
    async with _http() as http:
        await _rate(http, w.tenant, chat, "owner", "member")
        (resource,) = await _feedback_events(http, w.tenant, "admin")
    assert resource["agent_id"] == str(w.personal_agent)
    assert resource["department_id"] is not None
    assert resource["rating"] == "down"
    assert "note" not in resource


async def test_the_owner_sees_the_note_of_their_own_agents_feedback(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    chat = await _chat_with_answer(app_session, w.tenant, w.personal_agent, "owner")
    async with _http() as http:
        await _rate(http, w.tenant, chat, "owner", "member")
        (resource,) = await _feedback_events(http, w.tenant, "owner")
    assert resource["note"] == _NOTE


async def test_a_standard_agents_feedback_note_stays_visible(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    chat = await _chat_with_answer(app_session, w.tenant, w.standard_agent, "admin")
    async with _http() as http:
        await _rate(http, w.tenant, chat, "admin", "org_admin")
        (resource,) = await _feedback_events(http, w.tenant, "manager")
    assert resource["agent_id"] == str(w.standard_agent)
    assert resource["note"] == _NOTE


async def test_an_old_feedback_event_without_an_agent_keeps_its_note_from_others(
    app_session: AppSessionFactory,
) -> None:
    """Written before the event named its agent: which agent it was about is
    unknown, so it is read as personal -- only its writer sees the note."""
    w = await _world(app_session)
    chat = await _chat_with_answer(app_session, w.tenant, w.standard_agent, "owner")
    async with app_session(w.tenant) as db:
        await append_event(
            db,
            tenant_id=w.tenant,
            actor_type="operator",
            actor_id=await _member_id(db, "owner"),
            category="chat",
            action="chat.feedback",
            resource={
                "session_id": str(chat[0]),
                "message_id": str(chat[1]),
                "run_id": None,
                "rating": "down",
                "note": _NOTE,
            },
        )
    async with _http() as http:
        (as_admin,) = await _feedback_events(http, w.tenant, "admin")
        (as_writer,) = await _feedback_events(http, w.tenant, "owner")
    assert "note" not in as_admin
    assert as_admin["rating"] == "down"
    assert as_writer["note"] == _NOTE
