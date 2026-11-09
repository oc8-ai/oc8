"""A denied control tool stops; it does not merely get recorded as denied.

Every runtime hands a call to `execute_control_tool` BEFORE its own DENY
branch, and a non-None outcome skips that branch. So the dispatcher is where a
DENY for a core tool has to take effect -- a writing control tool that ignored
its decision used to run under /plan and /ask anyway: a file stored, a decision
request raised, a run parked, a shell result handed back to the model.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agent.control_tools import execute_control_tool
from oc8.agent.engine import _authorize
from oc8.authz.pdp import Effect
from oc8.chat.modes import MODES, WRITING_CONTROL_TOOLS
from oc8.modelrouter import ToolCall

#: Arguments that pass each writing tool's own validation, so a refusal can only
#: come from the decision -- not from a missing field.
_ARGS: dict[str, dict[str, Any]] = {
    "memory_write": {"content": "The customer prefers email.", "tier": "agent"},
    "delegate_task": {"agent_id": str(uuid.uuid4()), "task_text": "Check the invoice."},
    "write_output_file": {"filename": "plan.md", "content": "# Plan"},
    "propose_change": {"operation_type": "agent.update", "payload": {"name": "x"}},
    "decide_approval": {"approval_id": str(uuid.uuid4()), "decision": "approve"},
    "request_decision": {"question": "Refund it?", "context": "Order 42, damaged."},
    "ask_user": {"question": "Which order?"},
    "run_shell": {"command": "echo hi"},
    "run_program": {"code": "print('hi')"},
    "responsibility_open": {"title": "Chase the invoice"},
    "responsibility_update": {"responsibility_id": str(uuid.uuid4()), "note": "x"},
    "responsibility_close": {"responsibility_id": str(uuid.uuid4())},
    "schedule_followup": {"when": "2030-01-01T09:00:00Z", "note": "x"},
    "cancel_followup": {"followup_id": str(uuid.uuid4())},
}

_LOCAL = {"stdout": "hi\n", "stderr": "", "exit_code": 0, "timed_out": False}

#: Every table a writing control tool could leave a trace in.
_TRACE_TABLES = (
    m.FileAttachment,
    m.ApprovalRequest,
    m.AuditEvent,
    m.ActivityEvent,
    m.Task,
    m.AgentRun,
    m.Responsibility,
    m.MemoryRecord,
)


async def _seed(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task, m.AgentRun]:
    dept = m.Department(tenant_id=tenant, name="Sales", frame={})
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
        is_team_lead=True,
        # The Assistant is the one agent the propose/decide/Copilot tools serve,
        # so their own "only the Assistant" refusal cannot mask the decision.
        is_tenant_assistant=True,
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Plan the refund",
        state="in_progress",
        delegation_depth=0,
    )
    db.add(task)
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add(run)
    await db.flush()
    return agent, task, run


async def _traces(db: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for model in _TRACE_TABLES:
        out[model.__name__] = int(
            (await db.execute(select(func.count()).select_from(model))).scalar_one()
        )
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(WRITING_CONTROL_TOOLS))
async def test_a_writing_control_tool_under_plan_does_nothing(app_session: Any, tool: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _seed(db, tenant)
        tc = ToolCall(id="c1", name=tool, arguments=dict(_ARGS[tool]))
        decision = _authorize(
            agent,
            tc,
            frame={},
            tool_policies={},
            connection_key=None,
            tool_scopes=None,
            chat_mode=MODES["plan"],
        )
        assert decision.effect is Effect.DENY
        before = await _traces(db)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=tc,
            decision=decision,
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
            local_result=dict(_LOCAL) if tool in {"run_shell", "run_program"} else None,
        )
        await db.flush()
        after = await _traces(db)
    assert outcome is not None
    assert outcome.output.startswith("ERROR"), outcome.output
    assert "exit_code" not in outcome.output
    assert outcome.suspend is None
    assert outcome.pending_run is None
    assert after == before


@pytest.mark.asyncio
async def test_ask_stops_a_reading_control_tool_too(app_session: Any) -> None:
    """/ask withholds every tool, reads included -- and a reading control tool
    has no reason to look at its decision, so the dispatcher has to."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _seed(db, tenant)
        tc = ToolCall(id="c1", name="department_status", arguments={})
        decision = _authorize(
            agent,
            tc,
            frame={},
            tool_policies={},
            connection_key=None,
            tool_scopes=None,
            chat_mode=MODES["ask"],
        )
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=tc,
            decision=decision,
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
    assert outcome is not None
    assert outcome.output.startswith("ERROR")
    assert "/ask" in outcome.output


@pytest.mark.asyncio
async def test_a_reading_control_tool_still_runs_under_the_routine_frame_denial(
    app_session: Any,
) -> None:
    """A core tool with no early ALLOW is decided against the run's bound
    connection, which says nothing about it ("no grant for 'odoo'"). Read tools
    have always run through that verdict and must keep doing so."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run = await _seed(db, tenant)
        tc = ToolCall(
            id="c1",
            name="todo_write",
            arguments={"todos": [{"content": "Check the order", "status": "pending"}]},
        )
        decision = _authorize(
            agent,
            tc,
            frame={"tools": {}},
            tool_policies={},
            connection_key="odoo",
            tool_scopes=None,
        )
        assert decision.effect is Effect.DENY
        assert "no grant" in decision.reason
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=tc,
            decision=decision,
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
    assert outcome is not None
    assert not outcome.output.startswith("ERROR"), outcome.output
    assert outcome.todos == [{"content": "Check the order", "status": "pending"}]


@pytest.mark.parametrize("tool", sorted(WRITING_CONTROL_TOOLS))
def test_no_writing_control_tool_falls_through_to_a_frame_denial(tool: str) -> None:
    """The dispatcher refuses a writing control tool on ANY denial. So none of
    them may be decided against a connection it does not belong to -- or every
    call would be refused for "no grant for 'odoo'". A new writing tool has to
    get its own branch in `authorize`, and this is what says so."""
    agent = m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        narrowing={},
        definition={},
        presentation={},
        is_team_lead=True,
    )
    decision = _authorize(
        agent,
        ToolCall(id="c1", name=tool, arguments=dict(_ARGS[tool])),
        frame={"tools": {}},
        tool_policies={},
        connection_key="odoo",
        tool_scopes=None,
        chat_mode=None,
    )
    assert not (decision.effect is Effect.DENY and "no grant" in decision.reason), decision.reason


def _agent() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        narrowing={},
        definition={},
        presentation={},
    )


def test_plan_still_loads_a_skill() -> None:
    """A skill only hands the model a procedure. No connection scope names it,
    and it used to fail closed to "modify" -- a DENY nothing enforced. Now that
    a mode's DENY is enforced, it has to be decided as the read it is."""
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name="skill_refunds", arguments={}),
        frame={"tools": {}},
        tool_policies={},
        connection_key="odoo",
        tool_scopes=None,
        skill_tool_names=frozenset({"skill_refunds"}),
        chat_mode=MODES["plan"],
    )
    assert decision.effect is Effect.ALLOW
