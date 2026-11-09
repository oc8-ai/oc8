"""A slash command and a `#` reference, from `send_message`'s side (§5.2/§5.3).

The chat pipeline's own invariant is load-bearing here and is asserted rather
than assumed: a mode does not bypass `enqueue_run`. `/ask` still produces a
real AgentRun on the session's one shared Task -- it just produces one that
cannot call anything.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.chat.modes import ModeNotAvailable
from oc8.chat.service import create_session, send_message
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _session(db: object, tenant: uuid.UUID, *, copilot: bool = False) -> m.ChatSession:
    """A chat with an ordinary agent, or -- `copilot` -- with the tenant's
    Assistant, the only one that offers Agent mode."""
    if copilot:
        agent = await get_or_create_assistant(db, tenant_id=tenant)  # type: ignore[arg-type]
    else:
        dept = m.Department(tenant_id=tenant, name="Vertrieb")
        db.add(dept)  # type: ignore[attr-defined]
        await db.flush()  # type: ignore[attr-defined]
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", status="running")
        db.add(agent)  # type: ignore[attr-defined]
        await db.flush()  # type: ignore[attr-defined]
    member = m.OrgMember(
        tenant_id=tenant, all_departments=True, subject="op", subject_uuid=uuid.uuid4()
    )
    db.add(member)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return await create_session(db, tenant_id=tenant, agent_id=agent.id, member_id=member.id)  # type: ignore[arg-type]


async def test_a_command_is_recorded_as_a_mode_and_stripped_from_the_transcript(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/plan migrate the pipeline",
            originating_operator="op",
        )
        assert msg.mode == "plan"
        assert msg.content == "migrate the pipeline", "the command is not part of what was said"
        assert run is not None, "a mode is still a real run -- see chat/service.py's docstring"
        assert run.context["chat_mode"] == "plan"
        assert "[Mode: plan]" in run.context["task"]


async def test_the_session_still_shares_one_task(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The invariant a mode must not break."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _m1, run1 = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/ask what is open?",
            originating_operator="op",
        )
        _m2, run2 = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/do close them",
            originating_operator="op",
        )
        assert run1 is not None and run2 is not None
        assert run1.task_id == run2.task_id


async def test_plain_text_records_no_mode(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="wie viele Tickets sind offen?",
            originating_operator="op",
        )
        assert msg.mode is None
        assert run is not None
        assert "chat_mode" not in run.context
        assert "[Mode:" not in run.context["task"]


async def test_a_message_that_only_looks_like_a_command_is_sent_intact(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        msg, _run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/etc/passwd is world readable?",
            originating_operator="op",
        )
        assert msg.mode is None
        assert msg.content == "/etc/passwd is world readable?"


async def test_a_knowledge_base_reference_is_resolved_labelled_and_narrows_the_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name="Preisliste 2026", embedding_model="e")
        db.add(kb)
        await db.flush()
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="what is the standard discount?",
            context_refs=[{"kind": "knowledge_base", "id": str(kb.id)}],
            operator_role="org_admin",
            acting_member=await db.get(m.OrgMember, session.member_id),
            originating_operator="op",
        )
        assert msg.context_refs == [
            {"kind": "knowledge_base", "id": str(kb.id), "label": "Preisliste 2026"}
        ]
        assert run is not None
        assert run.context["context_kb_ids"] == [str(kb.id)]
        assert "Preisliste 2026" in run.context["task"]
        assert "search_knowledge" in run.context["task"]


async def test_an_ask_turn_is_not_told_to_call_a_tool_it_does_not_have(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """/ask offers no tools at all, so the attached knowledge base is named but
    the model is not told to search it -- that call could only be refused."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name="Preisliste 2026", embedding_model="e")
        db.add(kb)
        await db.flush()
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/ask what is the standard discount?",
            context_refs=[{"kind": "knowledge_base", "id": str(kb.id)}],
            operator_role="org_admin",
            acting_member=await db.get(m.OrgMember, session.member_id),
            originating_operator="op",
        )
        assert run is not None
        assert "Preisliste 2026" in run.context["task"]
        assert "search_knowledge" not in run.context["task"]


async def test_a_reference_to_something_that_does_not_exist_is_dropped(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Fail closed and visibly: the chip disappears rather than the turn being
    told to search a knowledge base nobody can read."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="what is the standard discount?",
            context_refs=[{"kind": "knowledge_base", "id": str(uuid.uuid4())}],
            operator_role="org_admin",
            acting_member=await db.get(m.OrgMember, session.member_id),
            originating_operator="op",
        )
        assert msg.context_refs == []
        assert run is not None
        assert "context_kb_ids" not in run.context


async def test_a_reference_of_an_unknown_kind_is_dropped(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        msg, _run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="x",
            context_refs=[{"kind": "odoo_record", "id": "42"}],
            operator_role="org_admin",
            acting_member=await db.get(m.OrgMember, session.member_id),
            originating_operator="op",
        )
        assert msg.context_refs == []


async def test_agent_command_makes_the_mode_stick(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, copilot=True)
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/agent build me an invoice checker",
            originating_operator="op",
        )
        assert msg.mode == "agent" and session.mode == "agent"
        assert run is not None and run.context["chat_mode"] == "agent"


async def test_a_plain_message_in_an_agent_session_runs_in_agent_mode(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, copilot=True)
        session.mode = "agent"
        await db.flush()
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="mornings, please",
            originating_operator="op",
        )
        # Recorded on the turn, so regenerate_reply (which reads msg.mode) keeps it.
        assert msg.mode == "agent"
        assert run is not None and "[Mode: agent]" in run.context["task"]


async def test_a_one_off_command_does_not_leave_the_sticky_mode(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, copilot=True)
        session.mode = "agent"
        await db.flush()
        msg, _run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/plan what next",
            originating_operator="op",
        )
        assert msg.mode == "plan" and session.mode == "agent"


async def test_a_followup_turn_ignores_the_sticky_mode(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, copilot=True)
        session.mode = "agent"
        await db.flush()
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="Follow-up for Offsite",
            originating_operator=None,
            role="followup",
        )
        assert msg.mode is None
        assert run is not None and "chat_mode" not in run.context


async def test_a_messenger_turn_neither_inherits_nor_sets_the_sticky_mode(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Ruling A7: the chip and its ✕ exist only on the web, so a messenger
    sender must never be stuck read-only -- `/agent` there is one turn."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant, copilot=True)
        session.mode = "agent"
        await db.flush()
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="mornings, please",
            originating_operator=None,
            on_behalf_of="op",
            chat_channel="telegram",
            chat_channel_external_id="42",
        )
        assert msg.mode is None
        assert run is not None and "chat_mode" not in run.context

        session.mode = None
        await db.flush()
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/agent build me an invoice checker",
            originating_operator=None,
            on_behalf_of="op",
            chat_channel="telegram",
            chat_channel_external_id="42",
        )
        assert msg.mode == "agent" and session.mode is None
        assert run is not None and run.context["chat_mode"] == "agent"


async def test_extra_context_cannot_set_the_mode(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Ruling 55: `chat_mode` never arrives through `extra_context`."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="hello",
            originating_operator="op",
            extra_context={"chat_mode": "agent", "mode": "agent"},
        )
        assert msg.mode is None and session.mode is None
        assert run is not None and "chat_mode" not in run.context


async def test_agent_mode_is_refused_outside_the_copilot_chat(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Its tools exist on the Assistant alone: an ordinary agent's chat neither
    takes `/agent ...` from the composer nor runs in a stored Agent mode."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        member = await db.get(m.OrgMember, session.member_id)
        with pytest.raises(ModeNotAvailable):
            await send_message(
                db,
                session=session,
                tenant_id=tenant,
                message="/agent build me an invoice checker",
                originating_operator="op",
                acting_member=member,
            )
        assert session.mode is None

        session.mode = "agent"  # stored before this rule
        await db.flush()
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="mornings, please",
            originating_operator="op",
        )
        assert msg.mode is None
        assert run is not None and "chat_mode" not in run.context


async def test_agent_outside_the_copilot_chat_is_plain_text_where_nobody_can_be_told(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """A carried-over run input has no composer to answer: what was written is
    sent as it was, like an unknown command."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="/agent build me an invoice checker",
            originating_operator="op",
        )
        assert msg.mode is None and msg.content == "/agent build me an invoice checker"
        assert session.mode is None
        assert run is not None and "chat_mode" not in run.context
