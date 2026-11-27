"""Temporary chats (spec §5.6): a conversation that is not kept.

Not listed, never named, never summarised, no notes about the person, no
feedback, no pin, project or export -- and deleted when the person leaves it
or at the latest after 24 hours. What the governance trail holds about it
(runs, approvals, audit events, token usage) stays."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agent.control_tools import offered_tools
from oc8.authz.pdp import Decision, Effect
from oc8.chat import checkpoint, port, titles
from oc8.chat.files_kb import chat_kb_for
from oc8.chat.service import _checkpoint_when_long, create_session, send_message
from oc8.chat.temporary import purge_temporary_chats
from tests.chat.test_edit_and_switch import _answer, _client, _post, _seed_agent, _token
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat
from tests.copilot.test_notes import _memory_tool, _notes_run

pytestmark = pytest.mark.asyncio


async def _new_chat(c: Any, tenant: uuid.UUID, agent_id: uuid.UUID, **body: Any) -> dict[str, Any]:
    r = await c.post(
        "/api/v1/chat/sessions", json={"agentId": str(agent_id), **body}, headers=_token(tenant)
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


# --- listing and reading ----------------------------------------------------


async def test_a_temporary_chat_is_not_listed_but_its_owner_reads_it(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        kept = await _new_chat(c, tenant, agent_id)
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        assert temp["temporary"] is True and kept["temporary"] is False
        listed = (await c.get("/api/v1/chat/sessions", headers=_token(tenant))).json()
        one = await c.get(f"/api/v1/chat/sessions/{temp['id']}", headers=_token(tenant))
        other = await c.get(
            f"/api/v1/chat/sessions/{temp['id']}", headers=_token(tenant, subject="someone-else")
        )
    assert [s["id"] for s in listed] == [kept["id"]]
    assert one.status_code == 200 and one.json()["temporary"] is True
    assert other.status_code == 404


async def test_the_chat_port_never_offers_a_temporary_chat(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        member = await db.get(m.OrgMember, seat.member_id)
        assert member is not None
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        temp = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=member.id, temporary=True
        )
        listed = {s.id for s in await port.list_sessions(db, member=member)}
        candidates = {s.id for s in await port.search_candidates(db, member=member)}
    assert seat.session_id in listed
    assert temp.id not in listed and temp.id not in candidates


# --- nothing is derived from it ----------------------------------------------


async def test_a_temporary_turn_is_never_named_and_its_run_knows(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        kept = await _new_chat(c, tenant, agent_id)
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        with patch("oc8.chat.service.schedule_auto_title") as named:
            await _post(c, tenant, kept["id"], "plan the offsite")
            assert named.call_count == 1, "an ordinary chat is named"
            sent = await _post(c, tenant, temp["id"], "plan the offsite")
            assert named.call_count == 1, "a temporary chat is not"
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(sent["runId"]))
        assert run is not None
        assert (run.context or {}).get("temporary_chat") is True
        session = await db.get(m.ChatSession, uuid.UUID(temp["id"]))
        assert session is not None and session.title == ""
        with patch.object(titles, "_model_title", side_effect=AssertionError("model called")):
            assert (
                await titles.generate_auto_title(
                    db, tenant_id=tenant, session_id=session.id, message="plan the offsite"
                )
                is None
            )


async def test_a_temporary_chat_starts_no_checkpoint_job(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        kept = await _new_chat(c, tenant, agent_id)
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        kept_turn = await _post(c, tenant, kept["id"], "long one")
        temp_turn = await _post(c, tenant, temp["id"], "long one")
    await _answer(app_session, tenant, kept_turn["runId"], "answer")
    await _answer(app_session, tenant, temp_turn["runId"], "answer")
    async with app_session(tenant) as db:
        huge = AsyncMock(return_value=10**9)
        for sid, turn, expected in (
            (kept["id"], kept_turn, 1),
            (temp["id"], temp_turn, 0),
        ):
            session = await db.get(m.ChatSession, uuid.UUID(sid))
            run = await db.get(m.AgentRun, uuid.UUID(turn["runId"]))
            assert session is not None and run is not None
            reply = (
                await db.execute(
                    select(m.ChatMessage).where(
                        m.ChatMessage.run_id == run.id, m.ChatMessage.role == "assistant"
                    )
                )
            ).scalar_one()
            started = MagicMock()
            with (
                patch.object(checkpoint, "replayed_estimate", huge),
                patch("oc8.realtime.emit.publish_after_commit", started),
            ):
                await _checkpoint_when_long(db, session=session, run=run, reply=reply)
            assert started.call_count == expected
        # The job itself refuses too, should one ever be scheduled.
        assert (
            await checkpoint.checkpoint_after_reply(
                db, session_id=uuid.UUID(temp["id"]), run_id=uuid.UUID(temp_turn["runId"])
            )
            is None
        )


async def test_memory_write_is_not_offered_in_a_temporary_chat() -> None:
    agent = m.Agent(id=uuid.uuid4(), tenant_id=uuid.uuid4(), department_id=uuid.uuid4())
    common: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}
    assert "memory_write" in {t.name for t in offered_tools(agent, **common)}
    assert "memory_write" not in {
        t.name for t in offered_tools(agent, **common, temporary_chat=True)
    }


async def test_a_temporary_chats_run_writes_no_memory(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        lisa = await copilot_seat(db, tenant, "lisa@example.com")
        task = await db.get(m.Task, lisa.task_id)
        assert task is not None
        run = await _notes_run(
            db, tenant, cop, lisa, door="web", originating_operator=lisa.subject,
            temporary_chat=True,
        )  # fmt: skip
        wrote = await _memory_tool(
            db, tenant, cop, task, run, "memory_write", {"tier": "agent", "content": "x"}
        )
        assert wrote.startswith("ERROR:") and "temporary" in wrote
        # A departmental agent's company write would park for an approval: it
        # is refused before one is raised.
        from oc8.agent.control_tools import execute_control_tool
        from oc8.modelrouter import ToolCall

        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        out = await execute_control_tool(
            db, tenant_id=tenant, agent=agent, task=task,
            tc=ToolCall(id="1", name="memory_write", arguments={"tier": "company", "content": "y"}),
            decision=Decision(Effect.REQUIRE_APPROVAL), assigned_skills=[], active_skills=[],
            mcp_conn=None, originating_operator=None, run_id=run.id, pinned=None,
        )  # fmt: skip
        assert out is not None and out.output.startswith("ERROR:")
        memories = await db.scalar(
            select(func.count())
            .select_from(m.MemoryRecord)
            .where(m.MemoryRecord.tenant_id == tenant)
        )
        approvals = await db.scalar(
            select(func.count())
            .select_from(m.ApprovalRequest)
            .where(m.ApprovalRequest.tenant_id == tenant)
        )
    assert memories == 0 and approvals == 0


# --- what a temporary chat cannot do ----------------------------------------


async def test_a_temporary_chat_cannot_be_kept_rated_or_exported(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        sid = temp["id"]
        turn = await _post(c, tenant, sid, "hello")
        reply = await _answer(app_session, tenant, turn["runId"], "hi")
        project = await c.post("/api/v1/chat/projects", json={"name": "P"}, headers=_token(tenant))
        assert project.status_code == 201, project.text
        answers = {
            "feedback": await c.post(
                f"/api/v1/chat/sessions/{sid}/messages/{reply}/feedback",
                json={"rating": "up"},
                headers=_token(tenant),
            ),
            "pinned": await c.put(
                f"/api/v1/chat/sessions/{sid}/pinned", json={"pinned": True}, headers=_token(tenant)
            ),
            "project": await c.put(
                f"/api/v1/chat/sessions/{sid}/project",
                json={"projectId": project.json()["id"]},
                headers=_token(tenant),
            ),
            "export_md": await c.get(
                f"/api/v1/chat/sessions/{sid}/export?format=md", headers=_token(tenant)
            ),
            "export_json": await c.get(
                f"/api/v1/chat/sessions/{sid}/export?format=json", headers=_token(tenant)
            ),
        }
    assert {k: r.status_code for k, r in answers.items()} == dict.fromkeys(answers, 409)
    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, uuid.UUID(sid))
        assert session is not None and not session.pinned and session.project_id is None
        rated = await db.scalar(
            select(func.count())
            .select_from(m.AuditEvent)
            .where(m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == "chat.feedback")
        )
    assert rated == 0


# --- leaving it, and the purge ----------------------------------------------


async def test_leaving_a_temporary_chat_removes_its_messages_files_and_knowledge(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        sid = uuid.UUID(temp["id"])
        turn = await _post(c, tenant, temp["id"], "read my file")
        await _answer(app_session, tenant, turn["runId"], "done")
        async with app_session(tenant) as db:
            session = await db.get(m.ChatSession, sid)
            assert session is not None
            kb = await chat_kb_for(db, session=session, create=True)
            assert kb is not None
            kb_id = kb.id
            db.add(
                m.FileAttachment(
                    tenant_id=tenant, owner_type="chat_message", owner_id=sid,
                    bucket_key=f"t/{uuid.uuid4()}", filename="notes.txt",
                    content_type="text/plain", size_bytes=3,
                )
            )  # fmt: skip
        r = await c.delete(f"/api/v1/chat/sessions/{sid}", headers=_token(tenant))
        assert r.status_code == 204, r.text
    async with app_session(tenant) as db:
        assert await db.get(m.ChatSession, sid) is None
        messages = await db.scalar(
            select(func.count()).select_from(m.ChatMessage).where(m.ChatMessage.session_id == sid)
        )
        files = await db.scalar(
            select(func.count())
            .select_from(m.FileAttachment)
            .where(m.FileAttachment.tenant_id == tenant)
        )
        base = await db.get(m.KnowledgeBase, kb_id)
        run = await db.get(m.AgentRun, uuid.UUID(turn["runId"]))
    assert messages == 0 and files == 0
    assert base is not None and base.deleted_at is not None, "the chat's base is retired"
    assert run is not None, "the run stays (governance)"


async def _chat_with_trail(
    db: Any, tenant: uuid.UUID, agent: m.Agent, *, temporary: bool, idle: dt.timedelta
) -> uuid.UUID:
    """A chat last active `idle` ago, with a run, an approval, an audit event
    and a usage record of its own."""
    from oc8.audit import append_event

    when = dt.datetime.now(dt.UTC) - idle
    session = await create_session(
        db, tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4(), temporary=temporary
    )
    session.created_at = when
    session.last_message_at = when
    db.add(m.ChatMessage(tenant_id=tenant, session_id=session.id, role="user", content="hi"))
    run = m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, source="chat", state="done",
        context={"chat_session_id": str(session.id)},
    )  # fmt: skip
    db.add(run)
    db.add(
        m.ApprovalRequest(
            tenant_id=tenant, agent_id=agent.id, action_type="tool_call", status="approved",
            payload={"run_id": str(run.id)},
        )
    )  # fmt: skip
    db.add(
        m.TokenUsageRecord(
            tenant_id=tenant, agent_id=agent.id, model="m", provider="p",
            request_id=uuid.uuid4(),
        )
    )  # fmt: skip
    await append_event(
        db, tenant_id=tenant, actor_type="operator", actor_id=None, category="chat",
        action="chat.test", resource={"chat_session_id": str(session.id)},
    )  # fmt: skip
    await db.flush()
    return session.id


async def test_purge_removes_temporary_chats_idle_for_a_day_and_keeps_the_trail(
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
        stale = await _chat_with_trail(
            db, tenant, agent, temporary=True, idle=dt.timedelta(hours=25)
        )
        fresh = await _chat_with_trail(
            db, tenant, agent, temporary=True, idle=dt.timedelta(hours=1)
        )
        old_kept = await _chat_with_trail(
            db, tenant, agent, temporary=False, idle=dt.timedelta(days=30)
        )
    async with app_session(tenant) as db:
        assert await purge_temporary_chats(db) == 1
    async with app_session(tenant) as db:
        remaining = set(
            (
                await db.execute(select(m.ChatSession.id).where(m.ChatSession.tenant_id == tenant))
            ).scalars()
        )
        stale_messages = await db.scalar(
            select(func.count()).select_from(m.ChatMessage).where(m.ChatMessage.session_id == stale)
        )
        runs = await db.scalar(
            select(func.count()).select_from(m.AgentRun).where(m.AgentRun.tenant_id == tenant)
        )
        approvals = await db.scalar(
            select(func.count())
            .select_from(m.ApprovalRequest)
            .where(m.ApprovalRequest.tenant_id == tenant)
        )
        usage = await db.scalar(
            select(func.count())
            .select_from(m.TokenUsageRecord)
            .where(m.TokenUsageRecord.tenant_id == tenant)
        )
        audit = await db.scalar(
            select(func.count())
            .select_from(m.AuditEvent)
            .where(m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == "chat.test")
        )
    assert remaining == {fresh, old_kept}
    assert stale_messages == 0
    assert (runs, approvals, usage, audit) == (3, 3, 3, 3), "governance is never purged"


async def test_purge_never_touches_an_ordinary_chat(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        kept = await _chat_with_trail(
            db, tenant, agent, temporary=False, idle=dt.timedelta(days=400)
        )
    async with app_session(tenant) as db:
        assert await purge_temporary_chats(db, older_than=dt.timedelta(0)) == 0
        assert await db.get(m.ChatSession, kept) is not None


async def test_a_messenger_message_never_lands_in_a_temporary_chat(
    app_session: AppSessionFactory,
) -> None:
    """The service list (which a messenger's next message picks its chat from)
    leaves temporary chats out, like the sidebar."""
    from oc8.chat.service import list_sessions

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        temp = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=seat.member_id, temporary=True
        )
        await send_message(
            db, session=temp, tenant_id=tenant, message="newest", originating_operator=seat.subject
        )
        listed = await list_sessions(db, tenant_id=tenant, member_id=seat.member_id)
    assert [s.id for s in listed] == [seat.session_id]
