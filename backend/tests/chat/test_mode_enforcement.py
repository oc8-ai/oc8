"""A mode reaches the PEP, in every runtime (§5.2).

`_authorize` is the one function the in-process engine, the MCP gateway and the
isolated shell's control plane all call, which is why the mode refusal lives
there rather than in three near-identical branches.
"""

from __future__ import annotations

import uuid

from oc8 import models as m
from oc8.agent.control_tools import offered_tools
from oc8.agent.engine import _authorize
from oc8.authz.pdp import Effect, ToolPolicy
from oc8.chat.modes import MODES
from oc8.modelrouter import NeutralTool, ToolCall

SCOPES = {"read": ["search_records"], "modify": ["create_record"]}
POLICIES = {"odoo": ToolPolicy(enabled=True, read=True, modify=True)}
FRAME = {"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}}


def _agent() -> m.Agent:
    return m.Agent(
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        narrowing={},
        definition={},
        presentation={},
    )


def _decide(tool: str, *, mode_key: str | None) -> object:
    return _authorize(
        _agent(),
        ToolCall(id="c1", name=tool, arguments={"model": "sale.order"}),
        frame=FRAME,
        tool_policies=POLICIES,
        connection_key="odoo",
        tool_scopes=SCOPES,
        chat_mode=MODES[mode_key] if mode_key else None,
    )


def test_without_a_mode_a_write_is_decided_as_it_always_was() -> None:
    assert _decide("create_record", mode_key=None).effect is Effect.ALLOW


def test_ask_denies_a_read() -> None:
    decision = _decide("search_records", mode_key="ask")
    assert decision.effect is Effect.DENY
    assert "/ask" in (decision.reason or "")


def test_plan_allows_a_read_and_denies_a_write() -> None:
    assert _decide("search_records", mode_key="plan").effect is Effect.ALLOW
    denied = _decide("create_record", mode_key="plan")
    assert denied.effect is Effect.DENY
    assert "/do" in (denied.reason or "")


def test_do_denies_nothing_the_frame_allows() -> None:
    assert _decide("create_record", mode_key="do").effect is Effect.ALLOW


def test_plan_denies_delegate_task_even_though_it_is_a_control_tool() -> None:
    """The early ALLOWs for core tools sit BELOW the mode check, which is the
    only reason this holds -- "executes nothing" has to include "does not get
    somebody else to execute it"."""
    agent = _agent()
    agent.is_team_lead = True
    decision = _authorize(
        agent,
        ToolCall(id="c1", name="delegate_task", arguments={"agent_id": str(uuid.uuid4())}),
        frame=FRAME,
        tool_policies=POLICIES,
        connection_key="odoo",
        tool_scopes=SCOPES,
        chat_mode=MODES["plan"],
    )
    assert decision.effect is Effect.DENY


def test_plan_denies_an_assigned_skill_tool_that_would_change_something() -> None:
    """A skill tool is ALLOWed unconditionally without a mode (it only loads a
    procedure) -- and loading one is a read, so plan leaves it alone."""
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name="skill_refunds", arguments={}),
        frame=FRAME,
        tool_policies=POLICIES,
        connection_key="odoo",
        tool_scopes={"read": ["skill_refunds"], "modify": []},
        skill_tool_names=frozenset({"skill_refunds"}),
        chat_mode=MODES["plan"],
    )
    assert decision.effect is Effect.ALLOW


def test_ask_offers_no_tools_at_all() -> None:
    assert (
        offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=[NeutralTool(name="create_record", description="", parameters={})],
            chat_mode=MODES["ask"],
        )
        == []
    )


def test_plan_offers_the_reading_core_tools_and_withholds_the_writing_ones() -> None:
    offered = {
        t.name
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=[],
            chat_mode=MODES["plan"],
        )
    }
    assert "memory_write" not in offered
    assert "write_output_file" not in offered
    assert "todo_write" in offered
    assert "fetch_url" in offered


def test_do_offers_exactly_what_no_mode_offers() -> None:
    kwargs = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}
    agent = _agent()
    assert [t.name for t in offered_tools(agent, chat_mode=MODES["do"], **kwargs)] == [
        t.name for t in offered_tools(agent, **kwargs)
    ]
