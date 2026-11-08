from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import SUGGEST_REPLIES, execute_control_tool, offered_tools
from oc8.agent.harness.stages.b_authorize import authorize
from oc8.authz.pdp import Decision, Effect
from oc8.chat.modes import MODES, WRITING_CONTROL_TOOLS, mode_refusal
from oc8.chat.service import record_assistant_reply
from oc8.modelrouter import ToolCall
from oc8.runtime.run_context import clean_suggestions
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_offered_in_plan_and_never_a_write() -> None:
    agent = m.Agent(tenant_id=uuid.uuid4(), department_id=uuid.uuid4(), name="A")
    names = {
        t.name
        for t in offered_tools(
            agent, assigned_skills=[], active_skills=[], mcp_tools=[], chat_mode=MODES["plan"]
        )
    }
    assert SUGGEST_REPLIES.name in names
    assert SUGGEST_REPLIES.name not in WRITING_CONTROL_TOOLS
    assert mode_refusal(MODES["plan"], SUGGEST_REPLIES.name, tool_scopes=None) is None
    assert (
        authorize(
            agent,
            ToolCall(id="1", name="suggest_replies", arguments={}),
            frame={},
            tool_policies={},
            connection_key=None,
            tool_scopes=None,
            chat_mode=MODES["plan"],
        ).effect
        is Effect.ALLOW
    )


async def test_clean_suggestions() -> None:
    assert clean_suggestions(["  Yes\x00\x07 ok ", "", "x" * 300, "b", "c"]) == [
        "Yes  ok",
        "x" * 120,
        "b",
    ]


async def test_suggestions_land_on_the_answer_and_are_consumed(
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
        task = m.Task(tenant_id=tenant, department_id=dept.id, title="t")
        db.add(task)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="chat",
            state="running",
            context={"chat_session_id": str(session.id)},
        )
        db.add(run)
        await db.flush()
        out = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name="suggest_replies",
                arguments={"options": ["Yes, send it", "Shorter", "  ", "Four", "Five"]},
            ),
            decision=Decision(effect=Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
        assert out is not None and not out.output.startswith("ERROR")
        run.state = "done"
        await record_assistant_reply(db, run=run, output="Draft ready.")
        await record_assistant_reply(db, run=run, output="Second answer.")
        replies = (
            (
                await db.execute(
                    select(m.ChatMessage)
                    .where(m.ChatMessage.session_id == session.id)
                    .order_by(m.ChatMessage.created_at, m.ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
    by_content = {r.content: r for r in replies}
    assert by_content["Draft ready."].parts == [
        {"type": "suggestions", "options": ["Yes, send it", "Shorter", "Four"]}
    ]
    assert not by_content["Second answer."].parts
