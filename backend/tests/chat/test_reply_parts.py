"""Edition parts reach the assistant message beside the core ones, cleaned at
that boundary and consumed so a later answer does not inherit them."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.chat.service import record_assistant_reply
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_edition_parts_merge_with_suggestions_and_are_consumed(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
        db.add(session)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="chat",
            state="done",
            context={
                "chat_session_id": str(session.id),
                "suggestions": ["Open it"],
                "parts": [
                    {"type": "document_ref", "id": "d1"},
                    {"type": "suggestions", "options": ["spoofed"]},
                    {"no_type": True},
                ],
            },
        )
        db.add(run)
        await db.flush()
        await record_assistant_reply(db, run=run, output="Here it is.")
        await record_assistant_reply(db, run=run, output="Second answer.")
        replies = (
            (await db.execute(select(m.ChatMessage).where(m.ChatMessage.session_id == session.id)))
            .scalars()
            .all()
        )
    by_content = {r.content: r for r in replies}
    assert by_content["Here it is."].parts == [
        {"type": "document_ref", "id": "d1"},
        {"type": "suggestions", "options": ["Open it"]},
    ]
    assert not by_content["Second answer."].parts


async def test_unusable_parts_never_fail_the_reply(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    deep: object = "leaf"
    for _ in range(50):
        deep = [deep]
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
        db.add(session)
        await db.flush()
        for raw in ({"type": "not-a-list"}, [{"type": "deep", "v": deep}], "text", 7):
            run = m.AgentRun(
                tenant_id=tenant,
                agent_id=agent.id,
                source="chat",
                state="done",
                context={"chat_session_id": str(session.id), "parts": raw},
            )
            db.add(run)
            await db.flush()
            await record_assistant_reply(db, run=run, output=f"reply {raw!r:.20}")
            assert "parts" not in {k for k, v in run.context.items() if v is not None}
        replies = (
            (await db.execute(select(m.ChatMessage).where(m.ChatMessage.session_id == session.id)))
            .scalars()
            .all()
        )
    assert len(replies) == 4
    assert all(not r.parts for r in replies)
