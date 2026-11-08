"""Structured `ask_user` (spec §4.1): up to six questions with options, parked
through the same `waiting_for_input` suspend as a plain question. The
structure travels in its own field on the outcome (`questions`), never inside
the readable output text."""

from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.agent.control_tools import ASK_USER, ControlOutcome, execute_control_tool
from oc8.authz.pdp import Decision, Effect
from oc8.modelrouter import ToolCall
from tests.conftest import AppSessionFactory


async def _call(app_session: AppSessionFactory, arguments: dict[str, Any]) -> ControlOutcome:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="A",
            definition={},
            presentation={},
            narrowing={},
        )
        db.add(agent)
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept.id,
            assigned_agent_id=agent.id,
            title="t",
            state="in_progress",
        )
        db.add(task)
        await db.flush()
        out = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name=ASK_USER.name, arguments=arguments),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert out is not None
    return out


def test_schema_offers_questions_and_no_longer_requires_question() -> None:
    params = ASK_USER.parameters
    assert "required" not in params or "question" not in params["required"]
    assert params["properties"]["questions"]["maxItems"] == 6
    item = params["properties"]["questions"]["items"]
    assert item["properties"]["options"]["maxItems"] == 8


async def test_structured_questions_suspend_with_a_dedicated_field(
    app_session: AppSessionFactory,
) -> None:
    out = await _call(
        app_session,
        {
            "questions": [
                {
                    "id": "r",
                    "prompt": "Region?",
                    "kind": "single",
                    "options": [{"key": "eu", "label": "EU"}],
                }
            ]
        },
    )
    assert out.suspend == "waiting_for_input"
    # Readable for every model and the inbox -- and nothing machine-only in it.
    assert out.output == "Region?\n- EU"
    assert out.questions is not None and out.questions[0]["id"] == "r"


async def test_too_many_questions_is_an_error_not_a_park(app_session: AppSessionFactory) -> None:
    out = await _call(
        app_session,
        {"questions": [{"id": f"q{i}", "prompt": "x", "kind": "text"} for i in range(7)]},
    )
    assert out.suspend is None and out.output.startswith("ERROR: ")
    assert out.questions is None


async def test_plain_question_is_unchanged(app_session: AppSessionFactory) -> None:
    out = await _call(app_session, {"question": "Order number?"})
    assert out.output == "Order number?" and out.suspend == "waiting_for_input"
    assert out.questions is None
