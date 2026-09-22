"""find_tools: rank deferred catalog cards and pin matches for the next step."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import (
    CONTROL_TOOL_NAMES,
    FIND_TOOLS,
    execute_control_tool,
    offered_tools,
)
from oc8.agent.harness.state import HarnessState
from oc8.authz.pdp import Decision, Effect
from oc8.modelrouter import NeutralTool, ToolCall

_CATALOG = [
    {
        "name": "list_inbox",
        "description": "List messages in the inbox.\nSecond line ignored.",
        "connection": "mail",
        "notes": "",
    },
    {
        "name": "create_invoice",
        "description": "Create a new invoice for a customer.",
        "connection": "billing",
        "notes": "",
    },
    {
        "name": "send_note",
        "description": "Send a short note to someone.",
        "connection": "mail",
        "notes": "",
    },
]


async def _agent_and_task(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task]:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Finder")
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=agent.department_id,
        assigned_agent_id=agent.id,
        title="Find a tool",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    return agent, task


@pytest.mark.asyncio
async def test_ranks_and_pins_without_duplicating(app_session: Any) -> None:
    tenant = uuid.uuid4()
    harness = HarnessState(tool_catalog=list(_CATALOG))
    async with app_session(tenant) as db:
        agent, task = await _agent_and_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name=FIND_TOOLS.name,
                arguments={"query": "invoice"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            harness_state=harness,
        )
        assert outcome is not None
        assert outcome.output == "create_invoice — Create a new invoice for a customer. (billing)"
        assert harness.pinned_tools == ["create_invoice"]

        again = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c2",
                name=FIND_TOOLS.name,
                arguments={"query": "invoice"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            harness_state=harness,
        )
        assert again is not None
        assert again.output == outcome.output
        assert harness.pinned_tools == ["create_invoice"]


@pytest.mark.asyncio
async def test_missing_catalog_says_everything_is_inline(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _agent_and_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name=FIND_TOOLS.name, arguments={"query": "anything"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            harness_state=None,
        )
    assert outcome is not None
    assert outcome.output == "No deferred tools. Every tool is already in your list."


def test_schema_registered_but_not_appended_by_offered_tools() -> None:
    assert FIND_TOOLS.name in CONTROL_TOOL_NAMES
    agent = m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        status="idle",
        definition={},
        presentation={},
    )
    names = [
        t.name
        for t in offered_tools(agent, assigned_skills=[], active_skills=[], mcp_tools=[])
    ]
    assert FIND_TOOLS.name not in names


def test_find_tools_is_a_neutral_tool_schema() -> None:
    assert isinstance(FIND_TOOLS, NeutralTool)
    assert FIND_TOOLS.parameters["properties"]["query"]["type"] == "string"
