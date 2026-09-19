"""write_output_file: the in-process "Standard" runtime's way to produce a
durable file during a run, since it has no /workspace mount (see
runtime/workspace.py for the containerized runtimes' equivalent, the synced
/workspace/output/ directory). Writes a FileAttachment(owner_type="agent_run",
owner_id=run.id) through the shared storage.attachments helper -- the same
one api/v1/files.py's uploads and the run-output sync both use."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import (
    WRITE_OUTPUT_FILE,
    execute_control_tool,
    offered_tools,
)
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
        title="Draft a report",
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
    assert WRITE_OUTPUT_FILE.name not in names


def test_offered_when_requested() -> None:
    names = [
        t.name
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=MCP_TOOLS,
            offer_write_output_file=True,
        )
    ]
    assert WRITE_OUTPUT_FILE.name in names


# ------------------------------------------------------------------ execution


@pytest.mark.asyncio
async def test_requires_an_active_run(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, _run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name="write_output_file",
                arguments={"filename": "report.txt", "content": "hello"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=None,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    assert "active run" in outcome.output


@pytest.mark.asyncio
async def test_writes_a_file_and_persists_it_under_the_run(
    app_session: Any, minio_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name="write_output_file",
                arguments={"filename": "report.txt", "content": "Q3 numbers look good."},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
        assert outcome is not None
        assert "Saved 'report.txt'" in outcome.output

        row = (
            await db.execute(
                select(m.FileAttachment).where(m.FileAttachment.filename == "report.txt")
            )
        ).scalar_one()
        assert row.owner_type == "agent_run"
        assert row.owner_id == run.id
        assert row.tenant_id == tenant
        assert row.content_type == "text/plain"
        assert row.extracted_text == "Q3 numbers look good."


@pytest.mark.asyncio
async def test_requires_a_filename(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="write_output_file", arguments={"content": "hello"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    assert "filename" in outcome.output


@pytest.mark.asyncio
async def test_requires_non_empty_content(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name="write_output_file",
                arguments={"filename": "report.txt", "content": ""},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    assert "content" in outcome.output


@pytest.mark.asyncio
async def test_rejects_an_unsupported_content_type(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name="write_output_file",
                arguments={
                    "filename": "data.json",
                    "content": "{}",
                    "content_type": "application/json",
                },
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    assert "content_type" in outcome.output
