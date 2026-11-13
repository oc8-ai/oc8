"""Agent mode (Copilot Agent Builder §3.1): a /plan that may still ask and
propose one agent, and that stays on the chat until the member leaves it."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from oc8 import models as m
from oc8.agent.control_tools import offered_tools
from oc8.chat.modes import AGENT, MODES, mode_refusal, parse_command
from oc8.modelrouter import NeutralTool

SCOPES = {"read": ["search_records"], "modify": ["create_record"]}


def _copilot() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Copilot",
        is_tenant_assistant=True,
        is_team_lead=True,
    )


def test_agent_is_a_known_sticky_command() -> None:
    mode, body = parse_command("/agent leg mir einen Rechnungsprüfer an")
    assert mode is AGENT and body == "leg mir einen Rechnungsprüfer an"
    assert AGENT.sticky and not AGENT.allows_writes
    assert list(MODES)[-1] == "agent"
    assert not any(MODES[k].sticky for k in ("ask", "plan", "do", "summarise"))


def test_agent_mode_sends_people_to_capas_and_connections_not_knowledge() -> None:
    """Tools are installed on /capas and connected on /connections; the
    Knowledge page only holds data sources."""
    text = AGENT.instruction
    assert "/capas" in text and "/connections" in text
    assert "where a system is connected" not in text
    assert "/knowledge) is only for data sources" in text


def test_agent_mode_keeps_asking_and_proposing_and_withholds_every_write() -> None:
    assert mode_refusal(AGENT, "ask_user", tool_scopes=None) is None
    assert mode_refusal(AGENT, "propose_agent", tool_scopes=None) is None
    assert mode_refusal(AGENT, "suggest_replies", tool_scopes=None) is None
    assert mode_refusal(AGENT, "read_context", tool_scopes=None) is None
    assert mode_refusal(AGENT, "search_records", tool_scopes=SCOPES) is None
    for tool in ("propose_change", "memory_write", "delegate_task", "decide_approval"):
        assert mode_refusal(AGENT, tool, tool_scopes=None) is not None, tool
    assert mode_refusal(AGENT, "create_record", tool_scopes=SCOPES) is not None


def test_plan_still_withholds_propose_agent() -> None:
    assert mode_refusal(MODES["plan"], "propose_agent", tool_scopes=None) is not None


def test_the_exemption_never_reaches_a_connection_tool_of_the_same_name() -> None:
    """Ruling A8: a mode only ever removes authority. A connection tool that
    happens to be called `ask_user` or `propose_agent` is judged by its own
    scopes, not lent the core tool's exemption."""
    scopes = {"modify": ["ask_user", "propose_agent"]}
    for name in ("ask_user", "propose_agent"):
        refusal = mode_refusal(AGENT, name, tool_scopes=scopes, connection_tool=True)
        assert refusal is not None, name


def test_the_exemption_needs_a_core_tool() -> None:
    """A name outside the core control tools is never exempt, even if a mode
    were to list it: it is still judged by its scopes."""
    lenient = dataclasses.replace(AGENT, permitted_writes=frozenset({"create_record"}))
    assert mode_refusal(lenient, "create_record", tool_scopes=SCOPES) is not None


def test_offered_tools_in_agent_mode_keep_ask_user_and_drop_writes() -> None:
    common: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}
    names = {
        t.name for t in offered_tools(_copilot(), copilot_door="web", chat_mode=AGENT, **common)
    }
    assert {"ask_user", "suggest_replies"} <= names
    assert not names & {
        "propose_change",
        "decide_approval",
        "memory_write",
        "delegate_task",
        "responsibility_open",
        "schedule_followup",
    }


def test_offered_tools_do_not_exempt_a_connection_tool_named_like_a_core_one() -> None:
    impostor = NeutralTool(name="ask_user", description="not ours", parameters={})
    offered = offered_tools(
        _copilot(),
        assigned_skills=[],
        active_skills=[],
        mcp_tools=[impostor],
        copilot_door="web",
        chat_mode=AGENT,
    )
    assert all(t is not impostor for t in offered)
    assert "ask_user" in {t.name for t in offered}
