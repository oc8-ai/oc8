"""read_run_file: an agent may read a file ANY run in its tenant produced
(FileAttachment(owner_type="agent_run")) -- content-level cross-agent access
is the whole point (unlike read_instruction_file, which is agent-scoped).
`run_id` narrows to one producing run when given; omitted, the newest match
across the tenant wins, same tiebreak read_instruction_file uses for a
repeated filename."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import (
    READ_RUN_FILE,
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
        title="Read a produced file",
        state="in_progress",
        delegation_depth=0,
    )
    db.add(task)
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add(run)
    await db.flush()
    return agent, task, run


# ------------------------------------------------------------------ offered_tools


def test_offered_unconditionally() -> None:
    """Unlike write_output_file (gated), read_run_file is in the base
    offered list for every runtime -- reading another run's file is useful
    even from a container that mounted its own /workspace."""
    names = [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=MCP_TOOLS)
    ]
    assert READ_RUN_FILE.name in names


# ------------------------------------------------------------------ execution


@pytest.mark.asyncio
async def test_resolves_a_file_produced_by_another_run(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
        _producer_agent, _producer_task, producing_run = await _dept_agent_task_run(db, tenant)
        db.add(
            m.FileAttachment(
                tenant_id=tenant,
                owner_type="agent_run",
                owner_id=producing_run.id,
                bucket_key="run-report-1",
                filename="report.txt",
                content_type="text/plain",
                size_bytes=10,
                extracted_text="Q3 numbers look good.",
                is_image=False,
            )
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="read_run_file", arguments={"filename": "report.txt"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert "Q3 numbers look good." in outcome.output


@pytest.mark.asyncio
async def test_run_id_narrows_to_one_producing_run(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
        _a1, _t1, run_a = await _dept_agent_task_run(db, tenant)
        _a2, _t2, run_b = await _dept_agent_task_run(db, tenant)
        db.add_all(
            [
                m.FileAttachment(
                    tenant_id=tenant,
                    owner_type="agent_run",
                    owner_id=run_a.id,
                    bucket_key="run-report-a",
                    filename="report.txt",
                    content_type="text/plain",
                    size_bytes=10,
                    extracted_text="From run A.",
                    is_image=False,
                ),
                m.FileAttachment(
                    tenant_id=tenant,
                    owner_type="agent_run",
                    owner_id=run_b.id,
                    bucket_key="run-report-b",
                    filename="report.txt",
                    content_type="text/plain",
                    size_bytes=10,
                    extracted_text="From run B.",
                    is_image=False,
                ),
            ]
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name="read_run_file",
                arguments={"filename": "report.txt", "run_id": str(run_a.id)},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert "From run A." in outcome.output


@pytest.mark.asyncio
async def test_an_invalid_run_id_is_rejected(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name="read_run_file",
                arguments={"filename": "report.txt", "run_id": "not-a-uuid"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    assert "not a valid run id" in outcome.output


@pytest.mark.asyncio
async def test_404s_an_unknown_filename(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="read_run_file", arguments={"filename": "nope.txt"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert "no such file" in outcome.output.lower()


@pytest.mark.asyncio
async def test_requires_a_filename(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="read_run_file", arguments={}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    assert "filename" in outcome.output


@pytest.mark.asyncio
async def test_refuses_an_image(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
        _p_agent, _p_task, producing_run = await _dept_agent_task_run(db, tenant)
        db.add(
            m.FileAttachment(
                tenant_id=tenant,
                owner_type="agent_run",
                owner_id=producing_run.id,
                bucket_key="run-chart-1",
                filename="chart.png",
                content_type="image/png",
                size_bytes=10,
                extracted_text=None,
                is_image=True,
            )
        )
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="read_run_file", arguments={"filename": "chart.png"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert "do not support vision" in outcome.output


@pytest.mark.asyncio
async def test_404s_a_foreign_tenants_file(app_session: Any) -> None:
    tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
    async with app_session(other_tenant) as other_db:
        _oa, _ot, other_run = await _dept_agent_task_run(other_db, other_tenant)
        other_db.add(
            m.FileAttachment(
                tenant_id=other_tenant,
                owner_type="agent_run",
                owner_id=other_run.id,
                bucket_key="run-secret-1",
                filename="secret.txt",
                content_type="text/plain",
                size_bytes=10,
                extracted_text="classified",
                is_image=False,
            )
        )
    async with app_session(tenant) as db:
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="read_run_file", arguments={"filename": "secret.txt"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert "no such file" in outcome.output.lower()


@pytest.mark.asyncio
async def test_survives_two_files_with_the_same_name_newest_wins(app_session: Any) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, reading_run = await _dept_agent_task_run(db, tenant)
        _a1, _t1, run_a = await _dept_agent_task_run(db, tenant)
        _a2, _t2, run_b = await _dept_agent_task_run(db, tenant)
        older = m.FileAttachment(
            tenant_id=tenant,
            owner_type="agent_run",
            owner_id=run_a.id,
            bucket_key="run-report-old",
            filename="report.txt",
            content_type="text/plain",
            size_bytes=10,
            extracted_text="Draft one.",
            is_image=False,
            created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        newer = m.FileAttachment(
            tenant_id=tenant,
            owner_type="agent_run",
            owner_id=run_b.id,
            bucket_key="run-report-new",
            filename="report.txt",
            content_type="text/plain",
            size_bytes=10,
            extracted_text="Final draft.",
            is_image=False,
            created_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        )
        db.add_all([older, newer])
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name="read_run_file", arguments={"filename": "report.txt"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=reading_run.id,
        )
    assert outcome is not None
    assert "Final draft." in outcome.output
