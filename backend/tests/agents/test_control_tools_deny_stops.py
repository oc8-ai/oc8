"""A denied control tool stops; it does not merely get recorded as denied.

Every runtime hands a call to `execute_control_tool` BEFORE its own DENY
branch, and a non-None outcome skips that branch. So the dispatcher is where a
DENY for a core tool has to take effect -- a writing control tool that ignored
its decision used to run under /plan and /ask anyway: a file stored, a decision
request raised, a run parked, a shell result handed back to the model.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agent.control_tools import execute_control_tool
from oc8.agent.engine import _authorize
from oc8.authz.pdp import Decision, Effect, ToolPolicy
from oc8.chat.modes import MODES, WRITING_CONTROL_TOOLS
from oc8.copilot.models import CopilotProposal
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
    m.Trigger,
    CopilotProposal,
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


_COPILOT_TOOLS = frozenset(
    {
        "responsibility_open",
        "responsibility_update",
        "responsibility_close",
        "schedule_followup",
        "cancel_followup",
    }
)
_SOON = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=30)).replace(hour=9).isoformat()


async def _live(
    db: Any, tenant: uuid.UUID, tool: str
) -> tuple[m.Agent, m.Task, uuid.UUID, dict[str, Any]]:
    """An agent, task, run and arguments under which `tool` really SUCCEEDS
    when ALLOWed -- real rows for every id it names. Without that, a refusal
    under /plan could come from a missing row instead of from the decision."""
    if tool in _COPILOT_TOOLS:
        from tests.copilot.test_tools import _call, _setup

        cop, _seat, task, run = await _setup(db, tenant, door="web")
        if tool == "responsibility_open":
            return cop, task, run.id, {"title": "Offsite", "goal": "Keep it on track"}
        await _call(db, tenant, cop, task, run, "responsibility_open", title="t", goal="g")
        rid = str((await db.execute(select(m.Responsibility))).scalar_one().id)
        followup = {
            "responsibility_id": rid,
            "kind": "once",
            "run_at": _SOON,
            "timezone": "Europe/Berlin",
            "prompt": "p",
        }
        if tool == "schedule_followup":
            return cop, task, run.id, followup
        if tool == "cancel_followup":
            saved = await _call(db, tenant, cop, task, run, "schedule_followup", **followup)
            assert saved is not None and saved.rendered_component is not None
            return cop, task, run.id, {"followup_id": saved.rendered_component["props"]["id"]}
        if tool == "responsibility_update":
            return cop, task, run.id, {"responsibility_id": rid, "next_step": "Call them"}
        return cop, task, run.id, {"responsibility_id": rid, "state": "done", "reason": "x"}

    from tests.agents.test_control_tools import (
        _assistant_and_task,
        _human_behind,
        _pending_approval,
    )

    assistant, task = await _assistant_and_task(db, tenant)
    await _human_behind(db, tenant, task, all_departments=True)
    run = m.AgentRun(
        tenant_id=tenant, agent_id=assistant.id, task_id=task.id, state="running", context={}
    )
    db.add(run)
    await db.flush()
    args = dict(_ARGS[tool])
    if tool == "delegate_task":
        mate = m.Agent(
            tenant_id=tenant,
            department_id=assistant.department_id,
            name="Rico",
            status="idle",
            definition={},
            presentation={},
        )
        db.add(mate)
        await db.flush()
        args["agent_id"] = str(mate.id)
    elif tool == "decide_approval":
        approval = await _pending_approval(db, tenant, department_id=task.department_id)
        args["approval_id"] = str(approval.id)
    elif tool == "propose_change":
        args = {
            "operation_type": "department.create",
            "payload": {"name": "Support EU", "goal": "", "icon": "building"},
        }
    return assistant, task, run.id, args


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "plan"], ids=["allowed", "plan"])
@pytest.mark.parametrize("tool", sorted(WRITING_CONTROL_TOOLS))
async def test_a_writing_control_tool_under_plan_does_nothing(
    app_session: Any, request: pytest.FixtureRequest, tool: str, mode: str | None
) -> None:
    """`allowed` proves the arguments are live: ALLOWed, the same call works.
    `plan` is the point: the PEP's mode refusal comes back, and nothing in the
    database moved."""
    if tool == "write_output_file":
        request.getfixturevalue("minio_url")  # a real attachment store
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run_id, args = await _live(db, tenant, tool)
        tc = ToolCall(id="c1", name=tool, arguments=args)
        if mode is None:
            decision = Decision(Effect.ALLOW)
        else:
            decision = _authorize(
                agent,
                tc,
                frame={},
                tool_policies={},
                connection_key=None,
                tool_scopes=None,
                chat_mode=MODES[mode],
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
            run_id=run_id,
            local_result=dict(_LOCAL) if tool in {"run_shell", "run_program"} else None,
        )
        await db.flush()
        after = await _traces(db)
    assert outcome is not None
    if mode is None:
        assert not outcome.output.startswith("ERROR"), outcome.output
        return
    assert outcome.output.startswith("ERROR"), outcome.output
    assert "/plan" in outcome.output, outcome.output
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


# ------------------------------------------------------------------ routed names


_ODOO = {"odoo": ToolPolicy(enabled=True, read=True, modify=True)}


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


def test_a_routed_connection_tool_does_not_borrow_a_core_tools_allowance() -> None:
    """`odoo.ask_user` is the CONNECTION's ask_user. Authorized by its bare name,
    it took the core tool's early ALLOW and ran on the connection's server with
    no frame grant at all."""
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name="ask_user", arguments={"question": "?"}),
        frame={"tools": {}},
        tool_policies={},
        connection_key="odoo",
        tool_scopes={"modify": ["ask_user"]},
        routed=True,
    )
    assert decision.effect is Effect.DENY


def test_a_bare_core_name_is_the_core_tool_whatever_a_connection_declares() -> None:
    """Routes always qualify a connection tool named like a core tool, so a
    bare core name can only ever reach the core tool -- and is decided as one,
    even when the bound connection's scopes happen to list the same name."""
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name="run_shell", arguments={"command": "ls"}),
        frame={"tools": {}},
        tool_policies={},
        connection_key="odoo",
        tool_scopes={"modify": ["run_shell"]},
    )
    assert decision.effect is Effect.ALLOW


def test_the_core_tool_itself_keeps_its_allowance() -> None:
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name="ask_user", arguments={"question": "?"}),
        frame={"tools": {}},
        tool_policies={},
        connection_key="odoo",
        tool_scopes=None,
        routed=False,
    )
    assert decision.effect is Effect.ALLOW


def test_plan_withholds_a_routed_connection_write_that_shares_a_reading_core_name() -> None:
    """`odoo.todo_write` is a write on odoo. Classified by its bare name it
    looked like the core to-do list -- a read -- and /plan let it through."""
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name="todo_write", arguments={}),
        frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
        tool_policies=_ODOO,
        connection_key="odoo",
        tool_scopes={"modify": ["todo_write"]},
        routed=True,
        chat_mode=MODES["plan"],
    )
    assert decision.effect is Effect.DENY


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


@pytest.mark.parametrize("tool", ["write_output_file", "request_decision", "ask_user"])
def test_a_qualified_core_name_never_gets_the_core_allowance(tool: str) -> None:
    """A runtime that authorizes the advertised name, unrouted, must not hand
    `odoo.write_output_file` the core tool's ALLOW either."""
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name=f"odoo.{tool}", arguments={}),
        frame={"tools": {}},
        tool_policies={},
        connection_key="odoo",
        tool_scopes=None,
    )
    assert decision.effect is Effect.DENY


def test_is_core_call_matches_what_authorize_decides() -> None:
    from oc8.agent.harness.stages.b_authorize import is_core_call

    assert is_core_call("write_output_file", routed=False)
    assert not is_core_call("write_output_file", routed=True)
    assert not is_core_call("odoo.write_output_file", routed=False)
