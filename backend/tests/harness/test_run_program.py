"""run_program offer gate and risk tier (Package 11)."""

from __future__ import annotations

import uuid

from oc8 import models as m
from oc8.agent.control_tools import offered_tools
from oc8.agent.harness.stages.b_risk_tier import classify_tier


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


def test_run_program_offer_gate_and_write_tier() -> None:
    withheld = {
        t.name: t
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=[],
            offer_run_shell=True,
            offer_run_program=False,
        )
    }
    assert "run_program" not in withheld

    # code_mode alone must not offer it when the shell gate is off
    shell_off = {
        t.name: t
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=[],
            offer_run_shell=False,
            offer_run_program=True,
        )
    }
    assert "run_program" not in shell_off

    offered = {
        t.name: t
        for t in offered_tools(
            _agent(),
            assigned_skills=[],
            active_skills=[],
            mcp_tools=[],
            offer_run_shell=True,
            offer_run_program=True,
        )
    }
    assert "run_program" in offered
    tool = offered["run_program"]
    required = tool.parameters.get("required", [])
    assert "code" in required
    assert "purpose" in required
    assert "code" in tool.parameters.get("properties", {})
    assert "purpose" in tool.parameters.get("properties", {})

    assert classify_tier("run_program", scopes=None, config=None, annotations=None) == "write"
    assert classify_tier("run_shell", scopes=None, config=None, annotations=None) == "write"
