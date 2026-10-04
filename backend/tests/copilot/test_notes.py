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
        for effect in (Effect.REQUIRE_APPROVAL, Effect.ALLOW):
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
