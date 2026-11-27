"""A rating counts for the model that wrote the rated answer.

The feedback event recorded the chat's model at rating time: a chat switched
to another model after the answer, or an Auto chat whose answer was routed to
a concrete model, credited the wrong one. The model now comes from the
answering run's usage records; the chat's model stands in only without them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from tests.api.test_personal_agent_content import _headers, _http, _subject_uuid, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _answered_chat(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    agent: uuid.UUID,
    usage: list[tuple[str, int]],
) -> tuple[uuid.UUID, uuid.UUID]:
    """A chat of `admin` whose answer came from a run that recorded `usage`
    (model, output tokens) -- each step in its own transaction, as a run's
    calls land between its creation and its answer."""
    async with app_session(tenant) as db:
        member = (
            await db.execute(
                select(m.OrgMember.id).where(m.OrgMember.subject_uuid == _subject_uuid("admin"))
            )
        ).scalar_one()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent, member_id=member)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent, state="done")
        db.add_all([session, run])
        await db.flush()
        ids = (session.id, run.id, member)
    for model, tokens_out in usage:
        async with app_session(tenant) as db:
            db.add(
                m.TokenUsageRecord(
                    tenant_id=tenant,
                    agent_id=agent,
                    member_id=ids[2],
                    model=model,
                    provider="openai",
                    tokens_out=tokens_out,
                    request_id=uuid.uuid4(),
                )
            )
    async with app_session(tenant) as db:
        answer = m.ChatMessage(
            tenant_id=tenant, session_id=ids[0], role="assistant", content="Done.", run_id=ids[1]
        )
        db.add(answer)
        await db.flush()
        return ids[0], answer.id


async def _switch_model(app_session: AppSessionFactory, tenant: uuid.UUID, sid: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        config = m.ModelConfig(
            tenant_id=tenant, provider="openai", model="switched-later", chat_enabled=True
        )
        db.add(config)
        await db.flush()
        session = await db.get(m.ChatSession, sid)
        assert session is not None
        session.model_override_id = config.id


async def _rated_model(
    app_session: AppSessionFactory, tenant: uuid.UUID, chat: tuple[uuid.UUID, uuid.UUID]
) -> object:
    async with _http() as http:
        r = await http.post(
            f"/api/v1/chat/sessions/{chat[0]}/messages/{chat[1]}/feedback",
            json={"rating": "up"},
            headers=_headers(tenant, "admin", "org_admin"),
        )
    assert r.status_code == 204, r.text
    async with app_session(tenant) as db:
        event = (
            await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == "chat.feedback"))
        ).scalar_one()
    return event.resource["model"]


async def test_a_vote_counts_for_the_model_that_answered_not_the_one_chosen_since(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    chat = await _answered_chat(app_session, w.tenant, w.standard_agent, [("answered", 400)])
    await _switch_model(app_session, w.tenant, chat[0])
    assert await _rated_model(app_session, w.tenant, chat) == "answered"


async def test_an_auto_routed_answer_counts_for_the_routed_model(
    app_session: AppSessionFactory,
) -> None:
    """The chat runs on Auto (no override): the usage records name the model
    the router picked. A title written in the same window does not win."""
    w = await _world(app_session)
    chat = await _answered_chat(
        app_session, w.tenant, w.standard_agent, [("routed-big", 300), ("title-small", 8)]
    )
    assert await _rated_model(app_session, w.tenant, chat) == "routed-big"


async def test_without_usage_the_chats_model_stands_in(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    chat = await _answered_chat(app_session, w.tenant, w.standard_agent, [])
    await _switch_model(app_session, w.tenant, chat[0])
    assert await _rated_model(app_session, w.tenant, chat) == "switched-later"
