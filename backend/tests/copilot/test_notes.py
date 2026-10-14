from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.copilot.notes import delete_note, list_notes
from oc8.memory.router import MemoryWriteError, retrieve_context, write_memory
from tests.conftest import AppSessionFactory

FRAME: dict[str, Any] = {"memory": {}}


async def test_notes_are_member_scoped(app_session: AppSessionFactory) -> None:
    tenant, lisa, max_ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        await write_memory(
            db,
            tenant_id=tenant,
            agent=cop,
            tier="agent",
            content="Lisa prefers updates in the morning",
            member_id=lisa,
        )
        ctx_max = await retrieve_context(
            db, agent=cop, tenant_id=tenant, frame=FRAME, query_text="updates", member_id=max_
        )
        ctx_lisa = await retrieve_context(
            db, agent=cop, tenant_id=tenant, frame=FRAME, query_text="updates", member_id=lisa
        )
        assert "Lisa prefers" not in ctx_max
        assert "Lisa prefers" in ctx_lisa


async def test_copilot_without_member_sees_no_personal_notes(
    app_session: AppSessionFactory,
) -> None:
    tenant, lisa = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        await write_memory(
            db, tenant_id=tenant, agent=cop, tier="agent", content="secret", member_id=lisa
        )
        assert "secret" not in await retrieve_context(
            db, agent=cop, tenant_id=tenant, frame=FRAME, query_text="secret", member_id=None
        )


async def test_copilot_cannot_write_shared_tiers(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        with pytest.raises(MemoryWriteError, match="personal"):
            await write_memory(
                db,
                tenant_id=tenant,
                agent=cop,
                tier="department",
                content="x",
                member_id=uuid.uuid4(),
            )
        with pytest.raises(MemoryWriteError, match="member"):
            await write_memory(
                db, tenant_id=tenant, agent=cop, tier="agent", content="x", member_id=None
            )


async def test_list_and_delete_own_note_only(app_session: AppSessionFactory) -> None:
    tenant, lisa, max_ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        rec = await write_memory(
            db, tenant_id=tenant, agent=cop, tier="agent", content="n", member_id=lisa
        )
        assert [
            n.id
            for n in await list_notes(db, tenant_id=tenant, member_id=lisa, assistant_id=cop.id)
        ] == [rec.id]
        assert (
            await delete_note(
                db, tenant_id=tenant, member_id=max_, assistant_id=cop.id, note_id=rec.id
            )
            is False
        )
        assert (
            await delete_note(
                db, tenant_id=tenant, member_id=lisa, assistant_id=cop.id, note_id=rec.id
            )
            is True
        )


async def test_search_memory_is_member_scoped(app_session: AppSessionFactory) -> None:
    """Review Focus 5: search_memory resolves the member behind the run's
    task, so Max's Copilot turn never recalls Lisa's note."""
    from oc8.agent.control_tools import execute_control_tool
    from oc8.authz.pdp import Decision, Effect
    from oc8.modelrouter import ToolCall
    from tests.copilot.helpers import copilot_seat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        lisa = await copilot_seat(db, tenant, "lisa@example.com")
        max_ = await copilot_seat(db, tenant, "max@example.com")
        await write_memory(
            db,
            tenant_id=tenant,
            agent=cop,
            tier="agent",
            content="Lisa prefers updates in the morning",
            member_id=lisa.member_id,
        )
        max_task = await db.get(m.Task, max_.task_id)
        lisa_task = await db.get(m.Task, lisa.task_id)
        assert max_task is not None and lisa_task is not None

        async def recall(task: m.Task, subject: str) -> str:
            out = await execute_control_tool(
                db,
                tenant_id=tenant,
                agent=cop,
                task=task,
                tc=ToolCall(id="1", name="search_memory", arguments={"query": "updates"}),
                decision=Decision(Effect.ALLOW),
                assigned_skills=[],
                active_skills=[],
                mcp_conn=None,
                originating_operator=subject,
                run_id=None,
                pinned=None,
            )
            assert out is not None
            return out.output

        assert "Lisa prefers" not in await recall(max_task, max_.subject)
        assert "Lisa prefers" in await recall(lisa_task, lisa.subject)


async def test_copilot_company_write_is_denied_at_authorization(
    app_session: AppSessionFactory,
) -> None:
    from oc8.agent.harness.stages.b_authorize import authorize
    from oc8.authz.pdp import Decision, Effect
    from oc8.memory.policy import authorize_memory_write
    from oc8.modelrouter import ToolCall

    assert authorize_memory_write({}, {}, "company").effect is Effect.REQUIRE_APPROVAL
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        other = m.Agent(tenant_id=tenant, name="Other", is_tenant_assistant=False)

        def decide(agent: m.Agent, tier: str) -> Decision:
            return authorize(
                agent,
                ToolCall(id="1", name="memory_write", arguments={"tier": tier, "content": "x"}),
                frame={},
                tool_policies={},
                connection_key=None,
                tool_scopes=None,
            )

        denied = decide(cop, "company")
        assert denied.effect is Effect.DENY
        assert denied.reason == "the Copilot keeps personal notes only (tier 'agent')"
        assert decide(cop, "department").effect is Effect.DENY
        assert decide(cop, "agent").effect is Effect.ALLOW
        assert decide(other, "company").effect is Effect.REQUIRE_APPROVAL


async def test_copilot_company_write_returns_error_not_approval(
    app_session: AppSessionFactory,
) -> None:
    from oc8.agent.control_tools import execute_control_tool
    from oc8.authz.pdp import Decision, Effect
    from oc8.modelrouter import ToolCall
    from tests.copilot.helpers import copilot_seat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        task = await db.get(m.Task, seat.task_id)
        assert task is not None
        # Defence in depth: authorization now denies, but a stray ALLOW or
        # REQUIRE_APPROVAL must still come back as ERROR, never raise.
        for effect in (Effect.DENY, Effect.REQUIRE_APPROVAL, Effect.ALLOW):
            out = await execute_control_tool(
                db,
                tenant_id=tenant,
                agent=cop,
                task=task,
                tc=ToolCall(
                    id="1", name="memory_write", arguments={"tier": "company", "content": "x"}
                ),
                decision=Decision(effect),
                assigned_skills=[],
                active_skills=[],
                mcp_conn=None,
                originating_operator=seat.subject,
                run_id=None,
                pinned=None,
            )
            assert out is not None and out.output.startswith("ERROR:")


async def test_legacy_untagged_copilot_note_is_invisible(app_session: AppSessionFactory) -> None:
    tenant, lisa = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        # A record written before member scoping existed: no member_id tag.
        rec = await write_memory(
            db, tenant_id=tenant, agent=cop, tier="agent", content="legacy note", member_id=lisa
        )
        rec.record_metadata = {}
        await db.flush()
        ctx = await retrieve_context(
            db,
            agent=cop,
            tenant_id=tenant,
            frame=FRAME,
            query_text="legacy",
            member_id=lisa,
        )
        assert "legacy note" not in ctx


async def _notes_run(db: Any, tenant: uuid.UUID, cop: m.Agent, seat: Any, **ctx: Any) -> m.AgentRun:
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=cop.id,
        source="chat",
        state="running",
        task_id=seat.task_id,
        context={"chat_session_id": str(seat.session_id), **ctx},
    )
    db.add(run)
    await db.flush()
    return run


async def _memory_tool(  # type: ignore[no-untyped-def]
    db, tenant, cop, task, run, name: str, arguments: dict[str, Any]
) -> str:
    from oc8.agent.control_tools import execute_control_tool
    from oc8.authz.pdp import Decision, Effect
    from oc8.modelrouter import ToolCall

    out = await execute_control_tool(
        db, tenant_id=tenant, agent=cop, task=task,
        tc=ToolCall(id="1", name=name, arguments=arguments),
        decision=Decision(Effect.ALLOW), assigned_skills=[], active_skills=[],
        mcp_conn=None, originating_operator=None, run_id=run.id, pinned=None,
    )  # fmt: skip
    assert out is not None
    return out.output


async def test_operator_in_a_colleagues_session_neither_reads_nor_writes_notes(
    app_session: AppSessionFactory,
) -> None:
    from oc8.copilot.notes import member_behind_run_task
    from tests.copilot.helpers import copilot_seat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        lisa = await copilot_seat(db, tenant, "lisa@example.com")
        await write_memory(
            db, tenant_id=tenant, agent=cop, tier="agent",
            content="Lisa prefers updates in the morning", member_id=lisa.member_id,
        )  # fmt: skip
        task = await db.get(m.Task, lisa.task_id)
        assert task is not None
        own = await _notes_run(db, tenant, cop, lisa, door="web", originating_operator=lisa.subject)
        oversight = await _notes_run(
            db, tenant, cop, lisa, door="web", originating_operator="admin@example.com"
        )
        assert (
            await member_behind_run_task(db, tenant_id=tenant, task=task, run_id=own.id)
            == lisa.member_id
        )
        assert (
            await member_behind_run_task(db, tenant_id=tenant, task=task, run_id=oversight.id)
            is None
        )
        recalled = await _memory_tool(
            db, tenant, cop, task, oversight, "search_memory", {"query": "updates"}
        )
        assert "Lisa prefers" not in recalled
        wrote = await _memory_tool(
            db, tenant, cop, task, oversight, "memory_write", {"tier": "agent", "content": "x"}
        )
        assert wrote.startswith("ERROR:")
        notes = await list_notes(
            db, tenant_id=tenant, member_id=lisa.member_id, assistant_id=cop.id
        )
        assert [n.content for n in notes] == ["Lisa prefers updates in the morning"]


async def test_followup_cannot_write_personal_notes(app_session: AppSessionFactory) -> None:
    from tests.copilot.helpers import copilot_seat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        lisa = await copilot_seat(db, tenant, "lisa@example.com")
        task = await db.get(m.Task, lisa.task_id)
        assert task is not None
        carried = {"responsibility_id": str(uuid.uuid4()), "trigger_id": str(uuid.uuid4())}
        fu = await _notes_run(
            db, tenant, cop, lisa, door="followup", followup=carried,
            originating_operator=lisa.subject,
        )  # fmt: skip
        wrote = await _memory_tool(
            db, tenant, cop, task, fu, "memory_write", {"tier": "agent", "content": "x"}
        )
        assert wrote == "ERROR: a follow-up cannot write personal notes"
        assert (
            await list_notes(db, tenant_id=tenant, member_id=lisa.member_id, assistant_id=cop.id)
            == []
        )
        web = await _notes_run(db, tenant, cop, lisa, door="web", originating_operator=lisa.subject)
        ok = await _memory_tool(
            db, tenant, cop, task, web, "memory_write", {"tier": "agent", "content": "y"}
        )
        assert ok.startswith("memory recorded")


async def _research_setup(db: Any, tenant: uuid.UUID, **ctx: Any) -> Any:
    from tests.copilot.test_tools import _setup

    return await _setup(db, tenant, **ctx)


async def _memory_write(
    db: Any, tenant: uuid.UUID, cop: m.Agent, task: m.Task, run: m.AgentRun, content: str
) -> Any:
    from oc8.agent.control_tools import execute_control_tool
    from oc8.authz.pdp import Decision, Effect
    from oc8.modelrouter import ToolCall

    out = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=cop,
        task=task,
        tc=ToolCall(id="1", name="memory_write", arguments={"tier": "agent", "content": content}),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=run.id,
        pinned=None,
    )
    assert out is not None
    return out


async def test_research_followup_writes_a_tagged_note(app_session: AppSessionFactory) -> None:
    from sqlalchemy import select

    from tests.copilot.test_tools import _chat_run

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop, seat, task, _ = await _research_setup(db, tenant, door="web")
        r_id = str(uuid.uuid4())
        run = await _chat_run(
            db, tenant, cop, seat, door="followup", chat_mode="research",
            followup={"responsibility_id": r_id, "trigger_id": "t", "turn_id": "turn-1"},
        )  # fmt: skip
        out = await _memory_write(db, tenant, cop, task, run, "Kunde X: neue Bestellung 12.10.")
        assert not out.output.startswith("ERROR"), out.output
        rec = (await db.execute(select(m.MemoryRecord))).scalar_one()
        assert rec.record_metadata["member_id"] == str(seat.member_id)
        assert rec.record_metadata["responsibility_id"] == r_id


async def test_check_in_followup_still_cannot_write_notes(app_session: AppSessionFactory) -> None:
    from tests.copilot.test_tools import _chat_run

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop, seat, task, _ = await _research_setup(db, tenant, door="web")
        run = await _chat_run(
            db, tenant, cop, seat, door="followup",
            followup={"responsibility_id": str(uuid.uuid4()), "trigger_id": "t", "turn_id": "x"},
        )  # fmt: skip
        out = await _memory_write(db, tenant, cop, task, run, "anything")
        assert out.output == "ERROR: a follow-up cannot write personal notes"


async def test_research_note_is_invisible_to_another_member(
    app_session: AppSessionFactory,
) -> None:
    from tests.copilot.helpers import copilot_seat
    from tests.copilot.test_tools import _chat_run

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop, seat, task, _ = await _research_setup(db, tenant, door="web")
        tom = await copilot_seat(db, tenant, "tom@example.com")
        run = await _chat_run(
            db, tenant, cop, seat, door="followup", chat_mode="research",
            followup={"responsibility_id": str(uuid.uuid4()), "trigger_id": "t", "turn_id": "t1"},
        )  # fmt: skip
        out = await _memory_write(db, tenant, cop, task, run, "private finding")
        assert not out.output.startswith("ERROR"), out.output
        assert (
            await list_notes(db, tenant_id=tenant, member_id=tom.member_id, assistant_id=cop.id)
            == []
        )
        mine = await list_notes(db, tenant_id=tenant, member_id=seat.member_id, assistant_id=cop.id)
        assert [n.content for n in mine] == ["private finding"]
