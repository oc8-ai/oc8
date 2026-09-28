"""procedure_step_done: mark manual procedure steps with evidence."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import (
    CONTROL_TOOL_NAMES,
    PROCEDURE_STEP_DONE,
    execute_control_tool,
    offered_tools,
)
from oc8.agent.harness.stages.c_ledger import (
    mark_decision_answered,
    record_decision,
    record_tool,
)
from oc8.agent.harness.state import CONTEXT_KEY, DecisionRef, HarnessState, ProcedureMark
from oc8.authz.pdp import Decision, Effect
from oc8.modelrouter import NeutralTool, ToolCall
from oc8.models.run import Clarification
from oc8.runtime.clarification import resolve_clarification
from oc8.skills.runtime import LoadedSkill
from oc8.skills.schema import parse_definition


def _skill(
    slug: str,
    steps: list[dict[str, Any]],
    *,
    name: str | None = None,
) -> LoadedSkill:
    definition = parse_definition(
        {
            "oc8_skill": 2,
            "id": slug,
            "version": "1.0.0",
            "instruction": "Follow the procedure.",
            "steps": steps,
        }
    )
    return LoadedSkill(
        skill_id=uuid.uuid4(),
        skill_version_id=uuid.uuid4(),
        name=name or slug,
        description="d",
        tool_name=f"skill_{slug}",
        definition=definition,
        creator_id=None,
    )


def _manual_quote_skill(slug: str = "quote-proc") -> LoadedSkill:
    return _skill(
        slug,
        [
            {
                "id": "confirm_scope",
                "title": "Confirm scope manually",
                "requires": {"manual": True},
            },
            {
                "id": "quote",
                "title": "Create the quotation",
                "requires": {"tool_called": "create_quotation"},
                "gates": ["create_quotation"],
            },
        ],
    )


async def _agent_and_task(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task]:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Proc")
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=agent.department_id,
        assigned_agent_id=agent.id,
        title="Mark a step",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    return agent, task


def test_record_tool_appends_once() -> None:
    from oc8.agent.harness.state import Ledger

    ledger = Ledger()
    record_tool(ledger, "create_quotation")
    record_tool(ledger, "create_quotation")
    assert ledger.tools_called == ["create_quotation"]


def test_mark_decision_answered_flips_last_unanswered() -> None:
    from oc8.agent.harness.state import Ledger

    ledger = Ledger(
        decisions=[
            DecisionRef(tool="ask_user", question="first?", step=1, answered=True),
            DecisionRef(tool="ask_user", question="second?", step=2, answered=False),
            DecisionRef(tool="ask_user", question="third?", step=3, answered=False),
        ]
    )
    mark_decision_answered(ledger)
    assert ledger.decisions[0].answered is True
    assert ledger.decisions[1].answered is False
    assert ledger.decisions[2].answered is True


def test_record_decision_leaves_answered_false() -> None:
    from oc8.agent.harness.state import Ledger

    ledger = Ledger()
    record_decision(ledger, tool="ask_user", question="Which?", step=1)
    assert ledger.decisions[0].answered is False


@pytest.mark.asyncio
async def test_marks_manual_step_and_overwrites_evidence(app_session: Any) -> None:
    tenant = uuid.uuid4()
    skill = _manual_quote_skill()
    harness = HarnessState()
    async with app_session(tenant) as db:
        agent, task = await _agent_and_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name=PROCEDURE_STEP_DONE.name,
                arguments={
                    "step_id": "confirm_scope",
                    "evidence": "Requester confirmed 10% discount",
                },
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            harness_state=harness,
            active_procedure_skills=[skill],
        )
        assert outcome is not None
        assert outcome.output == "Procedure step confirm_scope marked done."
        mark = harness.procedure[skill.definition.slug]
        assert mark.evidence["confirm_scope"] == "Requester confirmed 10% discount"

        again = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c2",
                name=PROCEDURE_STEP_DONE.name,
                arguments={
                    "step_id": "confirm_scope",
                    "evidence": "Updated evidence",
                },
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            harness_state=harness,
            active_procedure_skills=[skill],
        )
        assert again is not None
        assert again.output == "Procedure step confirm_scope marked done."
        assert list(harness.procedure[skill.definition.slug].evidence) == ["confirm_scope"]
        assert (
            harness.procedure[skill.definition.slug].evidence["confirm_scope"]
            == "Updated evidence"
        )


@pytest.mark.asyncio
async def test_non_manual_step_errors_and_leaves_evidence(app_session: Any) -> None:
    tenant = uuid.uuid4()
    skill = _manual_quote_skill()
    harness = HarnessState(
        procedure={
            skill.definition.slug: ProcedureMark(
                evidence={"confirm_scope": "already marked"}
            )
        }
    )
    async with app_session(tenant) as db:
        agent, task = await _agent_and_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name=PROCEDURE_STEP_DONE.name,
                arguments={"step_id": "quote", "evidence": "should not store"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            harness_state=harness,
            active_procedure_skills=[skill],
        )
    assert outcome is not None
    assert (
        outcome.output
        == "ERROR: step quote is tracked by the system; do not mark it manually."
    )
    assert harness.procedure[skill.definition.slug].evidence == {
        "confirm_scope": "already marked"
    }


@pytest.mark.asyncio
async def test_ambiguous_skill_without_skill_arg_errors(app_session: Any) -> None:
    tenant = uuid.uuid4()
    a = _manual_quote_skill("proc-a")
    b = _manual_quote_skill("proc-b")
    harness = HarnessState()
    async with app_session(tenant) as db:
        agent, task = await _agent_and_task(db, tenant)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c1",
                name=PROCEDURE_STEP_DONE.name,
                arguments={"step_id": "confirm_scope", "evidence": "x"},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            harness_state=harness,
            active_procedure_skills=[a, b],
        )
    assert outcome is not None
    assert (
        outcome.output
        == "ERROR: procedure_step_done needs one matching active procedure."
    )
    assert harness.procedure == {}


def test_schema_registered_and_appended_by_offered_tools() -> None:
    assert PROCEDURE_STEP_DONE.name in CONTROL_TOOL_NAMES
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
    assert PROCEDURE_STEP_DONE.name in names


def test_procedure_step_done_is_a_neutral_tool_schema() -> None:
    assert isinstance(PROCEDURE_STEP_DONE, NeutralTool)
    props = PROCEDURE_STEP_DONE.parameters["properties"]
    assert props["step_id"]["type"] == "string"
    assert props["evidence"]["type"] == "string"
    assert props["skill"]["type"] == "string"
    assert set(PROCEDURE_STEP_DONE.parameters["required"]) == {"step_id", "evidence"}


@pytest.mark.asyncio
async def test_resolve_clarification_marks_harness_decision_answered(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        harness = HarnessState()
        record_decision(harness.ledger, tool="ask_user", question="which one?", step=2)
        assert harness.ledger.decisions[0].answered is False
        ctx: dict[str, Any] = {
            "pending_question": "which one?",
            "task": "t",
        }
        harness.store(ctx)
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="waiting_for_input",
            context=ctx,
        )
        db.add(run)
        await db.flush()
        db.add(
            Clarification(
                tenant_id=tenant,
                run_id=run.id,
                agent_id=agent.id,
                question="which one?",
                status="open",
            )
        )
        await db.flush()
        await resolve_clarification(db, run=run, answer="the blue one")
        loaded = HarnessState.from_run_context(run.context)
        assert loaded.ledger.decisions[0].answered is True
        assert CONTEXT_KEY in run.context
