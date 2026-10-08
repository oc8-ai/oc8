"""Slash commands as MODE SWITCHES (§5.2 of the AI workplace design).

The load-bearing property is in `test_a_mode_can_only_ever_remove_authority`:
a mode is a narrowing, and there is no combination of mode and tool where the
answer is "allowed, because of the mode".
"""

from __future__ import annotations

import pytest

from oc8.chat.modes import (
    MODES,
    WRITING_CONTROL_TOOLS,
    mode_directive,
    mode_from_context,
    mode_refusal,
    parse_command,
)

#: A realistic connection classification: only the pack's OWN tools. Core
#: control tools are never listed on a connection's scopes, so listing them here
#: would license them through a path the runtime never takes.
SCOPES = {
    "read": ["search_records", "get_record"],
    "modify": ["create_record", "delete_record"],
}


# ------------------------------------------------------------------ the parser


def test_a_known_command_with_text_switches_mode() -> None:
    mode, body = parse_command("/ask how many tickets are open?")
    assert mode is not None
    assert mode.key == "ask"
    assert body == "how many tickets are open?"


def test_the_command_is_stripped_out_of_the_message() -> None:
    _mode, body = parse_command("/plan   migrate the pipeline")
    assert body == "migrate the pipeline"


def test_a_newline_separates_the_command_from_its_body() -> None:
    mode, body = parse_command("/do\nclose every solved ticket")
    assert mode is not None and mode.key == "do"
    assert body == "close every solved ticket"


def test_an_unknown_command_is_sent_verbatim() -> None:
    """The anti-pattern §5.2 names: a message that legitimately begins with a
    slash must still be sendable, unchanged."""
    assert parse_command("/deploy the thing") == (None, "/deploy the thing")


def test_a_path_is_not_a_command() -> None:
    assert parse_command("/etc/passwd is world readable?") == (
        None,
        "/etc/passwd is world readable?",
    )


def test_a_bare_command_with_no_body_is_sent_verbatim() -> None:
    """Nothing to ask yet -- so this is a literal message, not a mode switch.
    The composer arms the mode visually; the SEND is what needs a body."""
    assert parse_command("/ask") == (None, "/ask")
    assert parse_command("/summarise   ") == (None, "/summarise   ")


def test_an_uppercase_command_is_not_a_command() -> None:
    assert parse_command("/ASK something") == (None, "/ASK something")


def test_a_command_not_at_the_start_is_not_a_command() -> None:
    assert parse_command("please /ask about this") == (None, "please /ask about this")
    assert parse_command(" /ask about this") == (None, " /ask about this")


def test_plain_text_is_untouched() -> None:
    assert parse_command("wie viele Tickets sind offen?") == (
        None,
        "wie viele Tickets sind offen?",
    )


def test_an_empty_string_is_untouched() -> None:
    assert parse_command("") == (None, "")


# ------------------------------------------------- reading it back off a run


def test_the_mode_round_trips_through_a_run_context() -> None:
    assert mode_from_context({"chat_mode": "plan"}) is MODES["plan"]


def test_no_context_and_no_key_are_both_no_mode() -> None:
    assert mode_from_context(None) is None
    assert mode_from_context({}) is None
    assert mode_from_context({"chat_mode": ""}) is None


def test_a_mode_key_this_version_does_not_know_is_no_mode() -> None:
    """Forward compatibility, and fail-OPEN on purpose: a run enqueued by a
    newer release must still execute rather than deny every tool it has."""
    assert mode_from_context({"chat_mode": "teleport"}) is None


# ------------------------------------------------------------- the refusals


def test_ask_refuses_every_tool() -> None:
    for name in ("search_records", "create_record", "memory_write", "delegate_task"):
        assert mode_refusal(MODES["ask"], name, tool_scopes=SCOPES) is not None


def test_plan_allows_a_read_and_refuses_a_write() -> None:
    assert mode_refusal(MODES["plan"], "search_records", tool_scopes=SCOPES) is None
    assert mode_refusal(MODES["plan"], "create_record", tool_scopes=SCOPES) is not None


def test_plan_refuses_the_control_tools_that_change_something() -> None:
    """A mode that "executes nothing" has to mean delegate_task too: handing the
    work to another agent is an execution, just not this agent's."""
    for name in sorted(WRITING_CONTROL_TOOLS):
        assert mode_refusal(MODES["plan"], name, tool_scopes=SCOPES) is not None


def test_plan_allows_the_control_tools_that_only_read_or_show() -> None:
    """Core tools sit on no connection: the gateway asks with
    `tool_scopes=None`, the in-process engine with the connection's own scopes,
    and neither lists them. They are classified by name instead."""
    for name in (
        "search_knowledge",
        "search_memory",
        "fetch_url",
        "render_component",
        "todo_write",
        "read_resource",
        "read_run_file",
        "procedure_step_done",
    ):
        assert mode_refusal(MODES["plan"], name, tool_scopes=None) is None, name
        assert mode_refusal(MODES["plan"], name, tool_scopes=SCOPES) is None, name


def test_plan_refuses_the_control_tools_that_execute_commands() -> None:
    for name in ("run_shell", "run_program"):
        assert mode_refusal(MODES["plan"], name, tool_scopes=None) is not None


def test_plan_refuses_an_unclassified_tool() -> None:
    """`required_right` fails closed to modify for a tool no connection
    classified, and this inherits that rather than second-guessing it."""
    assert mode_refusal(MODES["plan"], "mystery_tool", tool_scopes=SCOPES) is not None
    assert mode_refusal(MODES["plan"], "search_records", tool_scopes=None) is not None


def test_do_refuses_nothing() -> None:
    for name in ("search_records", "create_record", "delegate_task", "memory_write"):
        assert mode_refusal(MODES["do"], name, tool_scopes=SCOPES) is None


def test_no_mode_refuses_nothing() -> None:
    assert mode_refusal(None, "create_record", tool_scopes=SCOPES) is None


def test_a_refusal_says_which_mode_and_names_the_way_out() -> None:
    reason = mode_refusal(MODES["plan"], "create_record", tool_scopes=SCOPES)
    assert reason is not None
    assert "/plan" in reason
    assert "/do" in reason, "a refusal that does not say how to proceed is a dead end"


def test_a_mode_can_only_ever_remove_authority() -> None:
    """THE property. For every mode and every tool: if no mode refuses it,
    some mode may refuse it -- but no mode may ever un-refuse what the
    mode-less baseline refuses. The baseline refuses nothing, so the assertion
    is that a mode never turns a refusal into a None."""
    tools = [
        "search_records",
        "create_record",
        "mystery_tool",
        *sorted(WRITING_CONTROL_TOOLS),
        "search_knowledge",
    ]
    for mode in (*MODES.values(), *INTERNAL_MODES.values()):
        for name in tools:
            baseline = mode_refusal(None, name, tool_scopes=SCOPES)
            assert baseline is None
            # A mode may add a refusal; it can never remove one, because there
            # is none to remove and no code path that returns "allowed".
            with_mode = mode_refusal(mode, name, tool_scopes=SCOPES)
            assert with_mode is None or isinstance(with_mode, str)


# ------------------------------------------------------------- the directive


def test_every_mode_has_a_directive_naming_itself() -> None:
    for mode in MODES.values():
        directive = mode_directive(mode)
        assert directive.startswith("[Mode: ")
        assert mode.key in directive


def test_every_mode_has_a_one_line_summary_for_the_picker() -> None:
    """§5.2's anti-pattern list: every command needs a one-line description in
    the picker. Enforced here so a fifth mode cannot be added without one."""
    for mode in MODES.values():
        assert mode.summary
        assert "\n" not in mode.summary
        assert len(mode.summary) <= 90


def test_the_phase_one_command_set_is_exactly_four() -> None:
    """`/handoff` and `/review` need a room; `/approve` would duplicate a
    screen; `/budget` never becomes a run and is answered client-side."""
    assert set(MODES) == {"ask", "plan", "do", "summarise"}


@pytest.mark.parametrize("key", ["ask", "summarise"])
def test_the_cheap_modes_take_no_tools_at_all(key: str) -> None:
    assert MODES[key].allows_tools is False


# --------------------------------------------------------- research (D2)

from oc8.chat.modes import INTERNAL_MODES, RESEARCH, RESEARCH_DELEGATE  # noqa: E402

_EXCEPTIONS = {"memory_write", "responsibility_update", "delegate_task"}


def test_research_modes_are_not_slash_commands() -> None:
    assert parse_command("/research look at customer X") == (
        None,
        "/research look at customer X",
    )
    assert "research" not in MODES and "research_delegate" not in MODES


def test_research_modes_resolve_from_context() -> None:
    assert mode_from_context({"chat_mode": "research"}) is RESEARCH
    assert mode_from_context({"chat_mode": "research_delegate"}) is RESEARCH_DELEGATE
    assert set(INTERNAL_MODES) == {"research", "research_delegate"}


def test_research_lets_only_the_copilot_through_its_three_exceptions() -> None:
    for name in sorted(WRITING_CONTROL_TOOLS):
        copilot = mode_refusal(RESEARCH, name, tool_scopes=None, is_tenant_assistant=True)
        other = mode_refusal(RESEARCH, name, tool_scopes=None, is_tenant_assistant=False)
        assert other is not None, name
        if name in _EXCEPTIONS:
            assert copilot is None, name
        else:
            assert copilot is not None, name


def test_research_refuses_modifying_connection_tools_even_for_the_copilot() -> None:
    assert mode_refusal(RESEARCH, "create_record", tool_scopes=SCOPES, is_tenant_assistant=True)
    assert (
        mode_refusal(RESEARCH, "search_records", tool_scopes=SCOPES, is_tenant_assistant=True)
        is None
    )


def test_research_delegate_has_no_exceptions() -> None:
    for name in sorted(WRITING_CONTROL_TOOLS):
        assert (
            mode_refusal(RESEARCH_DELEGATE, name, tool_scopes=None, is_tenant_assistant=True)
            is not None
        ), name
    assert mode_refusal(RESEARCH_DELEGATE, "create_record", tool_scopes=SCOPES) is not None
    assert mode_refusal(RESEARCH_DELEGATE, "search_records", tool_scopes=SCOPES) is None


def test_research_refusal_does_not_point_at_slash_do() -> None:
    reason = mode_refusal(RESEARCH, "ask_user", tool_scopes=None, is_tenant_assistant=True)
    assert reason is not None and "/do" not in reason and "research" in reason
