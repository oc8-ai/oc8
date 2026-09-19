"""run_shell: the one control tool that does NOT execute anything itself --
isolated_shell.py already ran the command locally (see its own module
docstring) before this call ever reaches the control plane, so this dispatch
only formats the pre-computed result for the transcript. No local_result
means the call did not arrive from a runtime that runs it locally, which is
an error, not a silent no-op."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import RUN_SHELL, execute_control_tool, offered_tools
from oc8.authz.pdp import Decision, Effect
from oc8.modelrouter import NeutralTool, ToolCall

MCP_TOOLS: list[NeutralTool] = []


def _agent() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        status="idle",
        definition={},
        presentation={},
    )


async def _dept_agent_task_run(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task, m.AgentRun]:
    dept = m.Department(tenant_id=tenant, name="Compliance", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
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
        title="Build a report",
        state="in_progress",
        delegation_depth=0,
    )
    db.add(task)
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add(run)
    await db.flush()
    return agent, task, run


# ------------------------------------------------------------------ offered_tools


def test_not_offered_by_default() -> None:
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    assert RUN_SHELL.name not in names


def test_offered_when_requested() -> None:
    names = [
        t.name
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=MCP_TOOLS,
            offer_run_shell=True,
        )
    ]
    assert RUN_SHELL.name in names


# ------------------------------------------------------------------ execution


@pytest.mark.asyncio
async def test_records_a_precomputed_result_without_executing_anything(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="run_shell", arguments={"command": "echo hi"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
            local_result={"stdout": "hi\n", "stderr": "", "exit_code": 0, "timed_out": False},
        )
    assert outcome is not None
    assert "hi" in outcome.output
    assert "exit_code=0" in outcome.output


@pytest.mark.asyncio
async def test_reports_a_nonzero_exit_code(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="run_shell", arguments={"command": "exit 3"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
            local_result={"stdout": "", "stderr": "boom", "exit_code": 3, "timed_out": False},
        )
    assert outcome is not None
    assert "exit_code=3" in outcome.output
    assert "boom" in outcome.output


@pytest.mark.asyncio
async def test_reports_a_timeout(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="run_shell", arguments={"command": "sleep 999"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
            local_result={"stdout": "", "stderr": "", "exit_code": None, "timed_out": True},
        )
    assert outcome is not None
    assert "timed out" in outcome.output.lower()


@pytest.mark.asyncio
async def test_errors_when_no_local_result_is_given(app_session: Any) -> None:
    """A run_shell call with no local_result means it reached this dispatch
    without ever having been executed by isolated_shell.py. Failing loudly
    here is what stops a never-executed command from being recorded as if it
    succeeded."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="run_shell", arguments={"command": "echo hi"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
