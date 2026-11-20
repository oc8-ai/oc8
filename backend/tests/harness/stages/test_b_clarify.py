"""B4 clarification checkpoint — predicate, prompt, parse, substitute."""

from __future__ import annotations

from oc8.agent.harness.stages.b_clarify import (
    apply_clarification,
    clarification_prompt,
    parse_clarification,
    should_clarify,
    substitute_call,
)
from oc8.agent.harness.state import HarnessState
from oc8.modelrouter import ToolCall


def test_should_clarify_false_for_write_tier() -> None:
    assert (
        should_clarify(
            tier="write",
            autonomy="autonomous",
            granted=True,
            clarify_enabled=True,
            already_done=False,
        )
        is False
    )


def test_should_clarify_false_when_posture_would_ask() -> None:
    assert (
        should_clarify(
            tier="irreversible",
            autonomy="default",
            granted=True,
            clarify_enabled=True,
            already_done=False,
        )
        is False
    )


def test_should_clarify_false_for_outward_without_grant() -> None:
    assert (
        should_clarify(
            tier="outward",
            autonomy="autonomous",
            granted=False,
            clarify_enabled=True,
            already_done=False,
        )
        is False
    )


def test_should_clarify_false_when_disabled() -> None:
    assert (
        should_clarify(
            tier="irreversible",
            autonomy="autonomous",
            granted=True,
            clarify_enabled=False,
            already_done=False,
        )
        is False
    )


def test_should_clarify_false_when_already_done() -> None:
    assert (
        should_clarify(
            tier="irreversible",
            autonomy="autonomous",
            granted=True,
            clarify_enabled=True,
            already_done=True,
        )
        is False
    )


def test_should_clarify_true_for_irreversible_autonomous() -> None:
    assert (
        should_clarify(
            tier="irreversible",
            autonomy="autonomous",
            granted=False,
            clarify_enabled=True,
            already_done=False,
        )
        is True
    )


def test_should_clarify_true_for_outward_autonomous_granted() -> None:
    assert (
        should_clarify(
            tier="outward",
            autonomy="autonomous",
            granted=True,
            clarify_enabled=True,
            already_done=False,
        )
        is True
    )


def test_parse_clarification_none_is_proceed() -> None:
    assert parse_clarification("NONE") is None


def test_parse_clarification_lowercase_none_is_facts() -> None:
    assert parse_clarification("none") == "none"


def test_parse_clarification_empty_is_proceed() -> None:
    assert parse_clarification("") is None
    assert parse_clarification("   ") is None


def test_parse_clarification_keeps_multiline_facts() -> None:
    text = "Recipient email\nAmount in USD"
    assert parse_clarification(text) == text


def test_parse_clarification_caps_at_2000_chars() -> None:
    long = "x" * 3000
    assert parse_clarification(long) == "x" * 2000


def test_substitute_call_chat_uses_ask_user() -> None:
    tc = ToolCall(id="1", name="delete_record", arguments={"id": 7})
    facts = "Which record?"
    sub = substitute_call(tc, facts, chat=True)
    assert sub.name == "ask_user"
    assert sub.arguments == {"question": facts}


def test_substitute_call_non_chat_uses_request_decision() -> None:
    tc = ToolCall(id="1", name="send_note", arguments={"to": "ada@example.com"})
    facts = "Recipient email"
    sub = substitute_call(tc, facts, chat=False)
    assert sub.name == "request_decision"
    assert sub.arguments["question"] == facts
    assert sub.arguments["context"] == "Missing facts before send_note."
    assert sub.arguments["options"] == []


def test_clarification_prompt_message_order_and_body() -> None:
    call_json = '{"arguments": {"id": 7}, "name": "delete_record"}'
    messages = clarification_prompt(
        task_text="Remove the draft",
        run_context="# Run context\nagent: office",
        ledger_block="# Working state\nEntities: none",
        call_json=call_json,
    )
    assert len(messages) == 4
    assert messages[0].role == "user"
    assert messages[0].content == "Task:\nRemove the draft"
    assert messages[1].content == "# Run context\nagent: office"
    assert messages[2].content == "# Working state\nEntities: none"
    assert messages[3].role == "user"
    assert "Proposed action:" in messages[3].content
    assert call_json in messages[3].content
    assert "Answer exactly NONE if there is none" in messages[3].content


def test_harness_state_clarification_done_defaults_false() -> None:
    assert HarnessState().clarification_done is False
    assert HarnessState.from_dict({"version": 1}).clarification_done is False


def test_apply_clarification_sets_flag_on_none_and_keeps_call() -> None:
    tc = ToolCall(id="1", name="delete_record", arguments={"id": 7})
    state = HarnessState()
    out = apply_clarification(tc, "NONE", chat=False, state=state)
    assert state.clarification_done is True
    assert out is tc
    assert out.name == "delete_record"


def test_apply_clarification_substitutes_on_facts() -> None:
    tc = ToolCall(id="1", name="delete_record", arguments={"id": 7})
    state = HarnessState()
    out = apply_clarification(tc, "Which record id?", chat=True, state=state)
    assert state.clarification_done is True
    assert out.name == "ask_user"
    assert out.arguments == {"question": "Which record id?"}
    assert out.id == "1"


def test_outward_ledger_guard_follows_live_tool_after_substitute() -> None:
    """After B4 substitutes, live tool must not inherit the gated outward tier."""
    gated_tool_name = "send_email"
    tier = "outward"
    tc = ToolCall(id="1", name=gated_tool_name, arguments={"to": "a@b.c"})
    state = HarnessState()
    live = apply_clarification(tc, "What is the recipient?", chat=True, state=state)
    assert live.name != gated_tool_name
    assert not (tier == "outward" and live.name == gated_tool_name)

    kept = apply_clarification(
        ToolCall(id="2", name=gated_tool_name, arguments={"to": "a@b.c"}),
        "NONE",
        chat=False,
        state=HarnessState(),
    )
    assert tier == "outward" and kept.name == gated_tool_name
