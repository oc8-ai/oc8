"""Dead connections, skill scope, and one resource body."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oc8.agent.mcp_client import McpSession

from oc8.agent.control_tools import _execute_find_tools
from oc8.agent.harness.caps import ModelCaps, caps_for_active_skills
from oc8.agent.harness.state import HarnessState
from oc8.agent.harness.stages.b_authorize import authorize
from oc8.agent.offering import (
    notice_for_find,
    pin_skill_tools,
    resource_sentence,
    unavailable_sentence,
)
from oc8.authz.pdp import Effect, ToolPolicy
from oc8.modelrouter import ToolCall


def test_unavailable_sentence_names_the_connection_and_drops_the_stack() -> None:
    sentence = unavailable_sentence("jira", "connection refused\n  at socket")
    assert sentence.startswith("Connection jira is connected but offered no tools:")
    assert "connection refused at socket" in sentence
    assert "Do not reach it through the shell." in sentence


def test_find_tools_repeats_the_dead_connection_when_the_query_names_it() -> None:
    state = HarnessState(
        unavailable_connections=[{"name": "jira", "reason": "connection refused"}]
    )
    outcome = _execute_find_tools(
        ToolCall(id="1", name="find_tools", arguments={"query": "jira issues"}),
        state,
    )
    assert "Connection jira is connected but offered no tools" in outcome.output
    assert "connection refused" in outcome.output
    assert "No deferred tools" not in outcome.output


def test_find_tools_stays_quiet_about_a_connection_the_query_does_not_name() -> None:
    notice = notice_for_find(
        [{"name": "jira", "reason": "connection refused"}],
        "odoo quotations",
        None,
    )
    assert notice is None


def test_resource_sentence_lists_names_and_not_a_body() -> None:
    sentence = resource_sentence(
        [
            {
                "connection": "odoo",
                "name": "chart-of-accounts",
                "uri": "odoo://chart",
                "description": "The chart",
            }
        ]
    )
    assert "chart-of-accounts" in sentence
    assert "read_resource" in sentence
    assert "body of the chart" not in sentence


def test_pin_skill_tools_keeps_existing_pins() -> None:
    assert pin_skill_tools(["search_archive"], ["create_quotation", "search_archive"]) == [
        "search_archive",
        "create_quotation",
    ]


def test_code_mode_follows_the_active_skill_only() -> None:
    on = SimpleNamespace(definition=SimpleNamespace(code_mode=True))
    off = SimpleNamespace(definition=SimpleNamespace(code_mode=False))
    assert caps_for_active_skills(ModelCaps(), [off]).code_mode is False
    assert caps_for_active_skills(ModelCaps(), [on]).code_mode is True
    assert caps_for_active_skills(ModelCaps(code_mode=True), [off]).code_mode is True


@pytest.mark.asyncio
async def test_read_resource_returns_that_one_body() -> None:
    class _Server:
        async def read_resource(self, uri: str) -> SimpleNamespace:
            assert uri == "odoo://chart"
            return SimpleNamespace(contents=[SimpleNamespace(text="the chart itself")])

    session = McpSession("python3", ["-c", "pass"])
    session._session = _Server()  # type: ignore[assignment]
    assert await session.read_resource("odoo://chart") == "the chart itself"


def test_read_resource_needs_a_uri_and_the_connections_read_right() -> None:
    missing = authorize(
        None,  # type: ignore[arg-type]
        ToolCall(id="1", name="read_resource", arguments={"connection": "odoo"}),
        frame={},
        tool_policies={},
        connection_key=None,
        tool_scopes=None,
        narrowing={},
        is_team_lead=False,
    )
    assert missing.effect is Effect.DENY

    allowed = authorize(
        None,  # type: ignore[arg-type]
        ToolCall(
            id="2",
            name="read_resource",
            arguments={"connection": "odoo", "uri": "odoo://chart"},
        ),
        frame={},
        tool_policies={"odoo": ToolPolicy(enabled=True, read=True)},
        connection_key=None,
        tool_scopes=None,
        narrowing={},
        is_team_lead=False,
    )
    assert allowed.effect is Effect.ALLOW
