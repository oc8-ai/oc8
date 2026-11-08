"""A messenger reply answers the structured question its chat run parked on
(spec §4.1): `1,3` or free text, instead of starting a new turn."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.channels.binding import issue_code, redeem_code
from oc8.channels.dispatch import process_inbound
from oc8.chat.questions import parse_questions, render_questions_text
from oc8.chat.service import create_session
from oc8.runtime.clarification import request_clarification
from tests.channels.test_free_text_end_to_end import CHANNEL, FakeChannel, _grant_copilot_manage
from tests.conftest import AppSessionFactory

_QUESTIONS = parse_questions(
    {
        "questions": [
            {
                "id": "ch",
                "prompt": "Which channels?",
                "kind": "multi",
                "options": [
                    {"key": "mail", "label": "Mail"},
                    {"key": "sms", "label": "SMS"},
                    {"key": "push", "label": "Push"},
                ],
            }
        ]
    }
)


async def _bound_member_with_parked_run(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, structured: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant, subject="pat", all_departments=True, subject_uuid=uuid.uuid4()
        )
        db.add(member)
        await db.flush()
        row = await issue_code(
            db, tenant_id=tenant, user_id=uuid.uuid4(), channel=CHANNEL, member_id=member.id
        )
        code, member_id = row.code, member.id
        await db.commit()
    async with app_session(tenant) as db:
        await redeem_code(db, tenant_id=tenant, channel=CHANNEL, code=str(code), external_id="777")
        await db.commit()
    async with app_session(tenant) as db:
        reloaded = await db.get(m.OrgMember, member_id)
        assert reloaded is not None
        await _grant_copilot_manage(db, tenant_id=tenant, member=reloaded)
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        session = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=member_id
        )
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=assistant.id,
            source="chat",
            state="running",
            context={
                "chat_session_id": str(session.id),
                "chat_channel": CHANNEL,
                "chat_channel_external_id": "777",
                "followup": {"responsibility_id": None},
            },
        )
        db.add(run)
        await db.flush()
        if structured:
            await request_clarification(
                db, run=run, question=render_questions_text(_QUESTIONS), questions=_QUESTIONS
            )
        else:
            await request_clarification(db, run=run, question="Order number?")
        run_id, session_id = run.id, session.id
        await db.commit()
    return run_id, session_id


async def _say(app_session: AppSessionFactory, tenant: uuid.UUID, impl: Any, text: str) -> None:
    async with app_session(tenant) as db:
        await process_inbound(
            db,
            impl,
            tenant_id=tenant,
            channel=CHANNEL,
            update={"message": {"text": text, "from": {"id": 777}}},
        )


async def test_a_numbered_reply_answers_the_parked_question(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id, session_id = await _bound_member_with_parked_run(app_session, tenant, structured=True)
    impl = FakeChannel()

    with patch("oc8.runtime.intake.publish_run") as published:
        await _say(app_session, tenant, impl, "1,3")

    assert published.called
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.state == "queued"
        assert run.context["clarifications"][-1]["answers"] == {"ch": ["mail", "push"]}
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.tenant_id == tenant)))
            .scalars()
            .all()
        )
        assert len(runs) == 1, "the reply answers the parked run, it does not start a new one"
        messages = (
            (await db.execute(select(m.ChatMessage).where(m.ChatMessage.session_id == session_id)))
            .scalars()
            .all()
        )
        assert [msg.content for msg in messages] == ["Which channels?: Mail, Push"]
        assert messages[0].parts is not None and messages[0].parts[0]["type"] == "answers"
        audit = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "clarification.answered")
                )
            )
            .scalars()
            .all()
        )
        assert [e.resource["via"] for e in audit] == [CHANNEL]
    impl.say.assert_awaited()


async def test_a_reply_that_fits_nothing_asks_again_and_keeps_the_question(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    run_id, _sid = await _bound_member_with_parked_run(app_session, tenant, structured=True)
    impl = FakeChannel()

    with patch("oc8.runtime.intake.publish_run") as published:
        await _say(app_session, tenant, impl, "9")

    assert not published.called
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.state == "waiting_for_input"
    sent = impl.say.await_args
    assert sent is not None and "1) Mail" in str(sent.args) + str(sent.kwargs)


async def test_a_plain_park_keeps_starting_a_new_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Unchanged for a plain question: the sender was told to answer in oc8."""
    tenant = uuid.uuid4()
    run_id, _sid = await _bound_member_with_parked_run(app_session, tenant, structured=False)
    impl = FakeChannel()

    with patch("oc8.runtime.intake.publish_run"):
        await _say(app_session, tenant, impl, "4711")

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None and run.state == "waiting_for_input"
