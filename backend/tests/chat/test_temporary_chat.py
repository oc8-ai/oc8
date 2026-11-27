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
        assert await purge_temporary_chats(db, tenant_id=tenant) == 1
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
        assert await purge_temporary_chats(db, tenant_id=tenant, older_than=dt.timedelta(0)) == 0
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


# --- review round 1: delegations, readers, stopping, oversight --------------


async def _temporary_copilot_turn(db: Any, tenant: uuid.UUID) -> tuple[Any, m.Agent, m.AgentRun]:
    """A Copilot turn of a temporary chat, as `send_message` writes it."""
    cop = await get_or_create_assistant(db, tenant_id=tenant)
    lisa = await copilot_seat(db, tenant, "lisa@example.com")
    session = await db.get(m.ChatSession, lisa.session_id)
    assert session is not None
    session.temporary = True
    await db.flush()
    run = await _notes_run(
        db, tenant, cop, lisa, door="web", originating_operator=lisa.subject, temporary_chat=True
    )
    return lisa, cop, run


async def _other_agent(db: Any, tenant: uuid.UUID, name: str) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name=f"D-{name}", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name=name, is_team_lead=True)
    db.add(agent)
    await db.flush()
    return agent


async def _forced_memory_write(db: Any, tenant: uuid.UUID, agent: m.Agent, run: m.AgentRun) -> str:
    from oc8.agent.control_tools import execute_control_tool
    from oc8.modelrouter import ToolCall

    task = await db.get(m.Task, run.task_id)
    out = await execute_control_tool(
        db, tenant_id=tenant, agent=agent, task=task,
        tc=ToolCall(id="1", name="memory_write", arguments={"tier": "agent", "content": "x"}),
        decision=Decision(Effect.ALLOW), assigned_skills=[], active_skills=[],
        mcp_conn=None, originating_operator=None, run_id=run.id, pinned=None,
    )  # fmt: skip
    assert out is not None
    return out.output


async def test_a_delegation_chain_of_a_temporary_chat_keeps_no_notes(
    app_session: AppSessionFactory,
) -> None:
    from oc8.agent.control_tools import start_delegation
    from oc8.chat.temporary import TEMPORARY_CHAT_KEY, in_temporary_chat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _lisa, cop, run = await _temporary_copilot_turn(db, tenant)
        nora = await _other_agent(db, tenant, "Nora")
        otto = await _other_agent(db, tenant, "Otto")
        sub = await start_delegation(
            db, parent_run=run, delegator=cop, target=nora, task_text="look it up"
        )
        # The worker gives a sub-run its task when it starts it.
        sub_task = m.Task(
            tenant_id=tenant, department_id=nora.department_id, assigned_agent_id=nora.id,
            title="sub", delegation_depth=1,
        )  # fmt: skip
        db.add(sub_task)
        await db.flush()
        sub.task_id = sub_task.id
        await db.flush()
        subsub = await start_delegation(
            db, parent_run=sub, delegator=nora, target=otto, task_text="and this"
        )
        assert sub.context.get(TEMPORARY_CHAT_KEY) is True
        assert subsub.context.get(TEMPORARY_CHAT_KEY) is True
        # Un-droppable: a hop that lost the key is still recognised by its chat.
        subsub.context = {k: v for k, v in subsub.context.items() if k != TEMPORARY_CHAT_KEY}
        await db.flush()
        assert await in_temporary_chat(db, subsub.context)
        subsub.task_id = sub_task.id
        await db.flush()
        for agent, hop in ((nora, sub), (otto, subsub)):
            assert (await _forced_memory_write(db, tenant, agent, hop)).startswith("ERROR:")
        memories = await db.scalar(
            select(func.count())
            .select_from(m.MemoryRecord)
            .where(m.MemoryRecord.tenant_id == tenant)
        )
    assert memories == 0


async def test_the_wake_up_of_a_temporary_chat_keeps_no_notes(
    app_session: AppSessionFactory,
) -> None:
    from oc8.chat.temporary import TEMPORARY_CHAT_KEY
    from oc8.runtime.executor import _maybe_wake_parent
    from oc8.runtime.repository import RunRepository

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        lisa, cop, _run = await _temporary_copilot_turn(db, tenant)
        nora = await _other_agent(db, tenant, "Nora")

        async def wake() -> uuid.UUID | None:
            return await _maybe_wake_parent(
                db, repo=RunRepository(db), tenant_id=tenant, parent_task_id=lisa.task_id,
                delegation_depth=1, finished_agent_id=nora.id, sub_task_label="x",
                output="done", succeeded=True, mcp_conn=None,
                chat_session_id=str(lisa.session_id), door="web",
                originating_operator=lisa.subject, temporary_chat=True,
            )  # fmt: skip

        wake_id = await wake()
        assert wake_id is not None
        woken = await db.get(m.AgentRun, wake_id)
        assert woken is not None and woken.context.get(TEMPORARY_CHAT_KEY) is True
        assert (await _forced_memory_write(db, tenant, cop, woken)).startswith("ERROR:")
        # Once the person left (the chat is gone), nobody is woken for it.
        session = await db.get(m.ChatSession, lisa.session_id)
        await db.delete(session)
        await db.flush()
        assert await wake() is None


async def test_the_engine_refuses_memory_write_before_it_could_park(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company write would park the run for an approval; in a temporary chat
    -- recognised by its chat row, the key dropped -- it is refused."""
    from oc8.agent.engine import run_agent
    from oc8.modelrouter import ToolCall
    from tests.agent.test_engine_parallel_reads import (
        _fixture,
        _RecordingServer,
        _session_factory,
        _turn,
    )
    from tests.websearch.test_web_search_tool import _Stream

    recorder = _RecordingServer(delay_s=0.0)
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _session_factory(recorder, ["read_a"]))
    call = ToolCall(id="c1", name="memory_write", arguments={"tier": "company", "content": "x"})
    script = _Stream([_turn("", call), _turn("Ok.")])
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", script)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _conn = await _fixture(db, tenant, tool_scopes={"read": ["read_a"]})
        session = await create_session(
            db, tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4(), temporary=True
        )
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, state="running", source="chat",
            context={"task": "go", "chat_session_id": str(session.id)},
        )  # fmt: skip
        db.add(run)
        await db.flush()
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=None, run_id=run.id
        )
        approvals = await db.scalar(
            select(func.count())
            .select_from(m.ApprovalRequest)
            .where(m.ApprovalRequest.tenant_id == tenant)
        )
        memories = await db.scalar(
            select(func.count())
            .select_from(m.MemoryRecord)
            .where(m.MemoryRecord.tenant_id == tenant)
        )
    assert "memory_write" not in script.tools[0]
    assert result.status != "waiting_for_approval"
    assert (approvals, memories) == (0, 0)


async def test_the_mcp_gateway_hides_and_refuses_memory_write(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed
    from tests.api.test_mcp_gateway import _token as _run_token

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        session = await create_session(
            db, tenant_id=tenant, agent_id=agent_id, member_id=uuid.uuid4(), temporary=True
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "chat_session_id": str(session.id)}
        await db.flush()
    token = _run_token(tenant, agent_id, run_id)
    _code, listed = await _rpc(token, "tools/list")
    assert "memory_write" not in {t["name"] for t in listed["result"]["tools"]}
    _code, called = await _rpc(
        token,
        "tools/call",
        {"name": "memory_write", "arguments": {"tier": "agent", "content": "x"}},
    )
    assert called["result"]["isError"] is True
    async with app_session(tenant) as db:
        memories = await db.scalar(
            select(func.count())
            .select_from(m.MemoryRecord)
            .where(m.MemoryRecord.tenant_id == tenant)
        )
    assert memories == 0


async def test_the_context_picker_offers_no_temporary_chat_or_its_files(
    app_session: AppSessionFactory,
) -> None:
    from oc8.chat.context.base import viewer_for_member
    from oc8.chat.context.registry import RESOLVERS

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # No assigned role: the token's (org_admin) rights, files door included.
        member = m.OrgMember(
            tenant_id=tenant, all_departments=True, subject="op", subject_uuid=uuid.uuid4()
        )
        db.add(member)
        await db.flush()
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        kept = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=member.id
        )
        kept.title = "Offsite kept"
        temp = await create_session(
            db, tenant_id=tenant, agent_id=assistant.id, member_id=member.id, temporary=True
        )
        temp.title = "Offsite secret"
        for owner, name in ((kept.id, "kept-offsite.txt"), (temp.id, "secret-offsite.txt")):
            db.add(
                m.FileAttachment(
                    tenant_id=tenant, owner_type="chat_message", owner_id=owner,
                    bucket_key=f"t/{uuid.uuid4()}", filename=name,
                    content_type="text/plain", size_bytes=3,
                )
            )  # fmt: skip
        await db.flush()
        viewer = await viewer_for_member(db, member, token_role="org_admin")
        chats = RESOLVERS["chat_session"]
        files = RESOLVERS["file"]
        found_chats = [s.title for s in await chats.search(db, viewer, "Offsite", 10)]
        found_files = [f.filename for f in await files.search(db, viewer, "offsite", 10)]
        temp_loaded = await chats.load(db, tenant, str(temp.id))
        assert temp_loaded is not None
        pinnable = await chats.authorize(db, viewer, temp_loaded)
    assert found_chats == ["Offsite kept"]
    assert found_files == ["kept-offsite.txt"]
    assert pinnable is False, "a stored pin of a temporary chat never resolves"


async def _waiting_turn(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: str, *, state: str
) -> uuid.UUID:
    """Put a turn's run in `state` with a parking card and a queued message."""
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        run.state = state
        approval = m.ApprovalRequest(
            tenant_id=tenant, agent_id=run.agent_id, task_id=run.task_id,
            action_type="tool_send", status="pending", payload={"run_id": str(run.id)},
        )  # fmt: skip
        db.add(approval)
        db.add(
            m.RunInput(
                tenant_id=tenant, run_id=run.id, author_member_id=uuid.uuid4(),
                text="and then?", delivery="next_turn",
            )
        )  # fmt: skip
        db.add(
            m.TokenUsageRecord(
                tenant_id=tenant, agent_id=run.agent_id, model="m", provider="p",
                request_id=uuid.uuid4(),
            )
        )  # fmt: skip
        await db.flush()
        return approval.id


async def test_leaving_mid_answer_stops_the_run_and_keeps_the_trail(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    from oc8.chat.service import record_assistant_reply

    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        turn = await _post(c, tenant, temp["id"], "go")
        approval_id = await _waiting_turn(
            app_session, tenant, turn["runId"], state="waiting_for_approval"
        )
        r = await c.delete(f"/api/v1/chat/sessions/{temp['id']}", headers=_token(tenant))
        assert r.status_code == 204, r.text
    run_id = uuid.UUID(turn["runId"])
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        cancel = await db.scalar(
            select(m.RunCancellation.cancellation_kind).where(m.RunCancellation.run_id == run_id)
        )
        approval = await db.get(m.ApprovalRequest, approval_id)
        queued = (
            await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))
        ).scalar_one()
        # The worker ends it; its answer finds no chat to land in.
        run.state = "interrupted"
        await record_assistant_reply(db, run=run, output="too late")
        replies = await db.scalar(
            select(func.count()).select_from(m.ChatMessage).where(m.ChatMessage.tenant_id == tenant)
        )
        usage = await db.scalar(
            select(func.count())
            .select_from(m.TokenUsageRecord)
            .where(m.TokenUsageRecord.tenant_id == tenant)
        )
        stops = await db.scalar(
            select(func.count())
            .select_from(m.AuditEvent)
            .where(m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == "run.cancel")
        )
    assert cancel == "operator_interrupted"
    assert run.state == "interrupted"
    assert approval is not None and approval.status == "expired"
    assert queued.consumed_at is not None and queued.consumed_step == -3, "cancelled"
    assert replies == 0
    assert (usage, stops) == (1, 1)


async def test_purge_stops_a_parked_run_first_and_deletes_once_it_has_ended(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        turn = await _post(c, tenant, temp["id"], "go")
    approval_id = await _waiting_turn(
        app_session, tenant, turn["runId"], state="waiting_for_approval"
    )
    sid, run_id = uuid.UUID(temp["id"]), uuid.UUID(turn["runId"])
    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, sid)
        assert session is not None
        session.last_message_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)
    async with app_session(tenant) as db:
        # Woken by the stop (queued, for the worker to end): not deleted yet.
        assert await purge_temporary_chats(db, tenant_id=tenant) == 0
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        approval = await db.get(m.ApprovalRequest, approval_id)
        assert run is not None and run.state == "queued"
        assert approval is not None and approval.status == "expired"
        assert await db.get(m.ChatSession, sid) is not None
        run.state = "interrupted"  # the worker's before-start check
    async with app_session(tenant) as db:
        assert await purge_temporary_chats(db, tenant_id=tenant) == 1
    async with app_session(tenant) as db:
        assert await db.get(m.ChatSession, sid) is None
        assert await db.get(m.AgentRun, run_id) is not None
        stops = await db.scalar(
            select(func.count())
            .select_from(m.AuditEvent)
            .where(m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == "run.cancel")
        )
    assert stops == 1, "one stop, however many ticks"


async def test_purge_all_visits_every_tenant(app_session: AppSessionFactory) -> None:
    from oc8.chat.temporary import purge_all_temporary_chats

    tenants = [uuid.uuid4(), uuid.uuid4()]
    sessions = []
    for tenant in tenants:
        async with app_session(tenant) as db:
            db.add(
                m.Organization(
                    id=tenant, slug=f"t{tenant.hex[:8]}", name="T", tier="standard", region="eu"
                )
            )
            await db.flush()
            agent = await _other_agent(db, tenant, "A")
            sessions.append(
                await _chat_with_trail(
                    db, tenant, agent, temporary=True, idle=dt.timedelta(hours=48)
                )
            )
    assert await purge_all_temporary_chats() >= 2
    for tenant, sid in zip(tenants, sessions, strict=True):
        async with app_session(tenant) as db:
            assert await db.get(m.ChatSession, sid) is None


async def test_a_temporary_chat_is_its_owners_alone(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """No `copilot:manage` oversight into it, and no title."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        assistant_id = assistant.id
    async with _client() as c:
        kept = await _new_chat(c, tenant, assistant_id)
        temp = await _new_chat(c, tenant, assistant_id, temporary=True)
        admin = _token(tenant, subject="admin")
        seen = {
            k: (await c.get(f"/api/v1/chat/sessions/{s['id']}", headers=admin)).status_code
            for k, s in (("kept", kept), ("temp", temp))
        }
        renamed = await c.patch(
            f"/api/v1/chat/sessions/{temp['id']}", json={"title": "x"}, headers=_token(tenant)
        )
        removed = await c.delete(f"/api/v1/chat/sessions/{temp['id']}", headers=admin)
    assert seen == {"kept": 200, "temp": 404}
    assert renamed.status_code == 409
    assert removed.status_code == 404


async def test_the_purges_stop_is_audited_as_the_system(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Nobody pressed Stop: the purge's cancel is the system's, not an
    operator without a name."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        turn = await _post(c, tenant, temp["id"], "go")
    await _waiting_turn(app_session, tenant, turn["runId"], state="waiting_for_approval")
    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, uuid.UUID(temp["id"]))
        assert session is not None
        session.last_message_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)
    async with app_session(tenant) as db:
        await purge_temporary_chats(db, tenant_id=tenant)
    async with app_session(tenant) as db:
        actors = (
            await db.execute(
                select(m.AuditEvent.actor_type, m.AuditEvent.actor_id).where(
                    m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == "run.cancel"
                )
            )
        ).all()
    assert [tuple(a) for a in actors] == [("system", None)]


async def test_a_run_naming_a_chat_that_is_gone_counts_as_temporary(
    app_session: AppSessionFactory,
) -> None:
    """Fail closed: a missing chat row may be a temporary chat already
    deleted, so its late runs keep no notes either."""
    from oc8.chat.temporary import in_temporary_chat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await in_temporary_chat(db, {"chat_session_id": str(uuid.uuid4())})
        assert not await in_temporary_chat(db, {})


async def test_a_second_stop_wakes_a_parked_run_whose_first_wake_failed(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The cancel row is written even when the wake times out on a lock; a
    later stop (the purge's next tick, a second leave) must still close the
    card instead of skipping the run because it already has a cancel."""
    from oc8.chat.temporary import stop_chat_runs

    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        temp = await _new_chat(c, tenant, agent_id, temporary=True)
        turn = await _post(c, tenant, temp["id"], "go")
    approval_id = await _waiting_turn(
        app_session, tenant, turn["runId"], state="waiting_for_approval"
    )
    run_id = uuid.UUID(turn["runId"])
    async with app_session(tenant) as db:
        db.add(
            m.RunCancellation(
                tenant_id=tenant, run_id=run_id, requested_at=dt.datetime.now(dt.UTC),
                cancellation_kind="operator_interrupted",
            )
        )  # fmt: skip
    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, uuid.UUID(temp["id"]))
        assert session is not None
        await stop_chat_runs(db, session=session, actor_id=None, principal=None)
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        approval = await db.get(m.ApprovalRequest, approval_id)
    assert run is not None and run.state == "queued"
    assert approval is not None and approval.status == "expired"
