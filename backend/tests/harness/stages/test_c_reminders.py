"""C5: the three advisory reminders, frozen exactly as they behave today.
These strings reach the model; changing one is a prompt change and belongs to
a later package with an eval delta, not here."""

from __future__ import annotations

from oc8.agent.harness.stages.c_reminders import (
    MAX_TOOL_RESULT_CHARS,
    REPEAT_CALL_THRESHOLDS,
    TOOL_OUTPUT_BUDGET_WARNING_CHARS,
    cap_tool_output,
    tool_output_budget_reminder,
    track_repeat_tool_call,
)
from oc8.modelrouter import ToolCall


def test_constants_are_unchanged() -> None:
    assert MAX_TOOL_RESULT_CHARS == 20_000
    assert TOOL_OUTPUT_BUDGET_WARNING_CHARS == 150_000
    assert REPEAT_CALL_THRESHOLDS == (3, 5, 8)


def test_a_result_at_the_cap_is_untouched() -> None:
    text = "x" * MAX_TOOL_RESULT_CHARS
    assert cap_tool_output(text) is text


def test_an_oversized_result_is_cut_with_the_omitted_count() -> None:
    text = "x" * (MAX_TOOL_RESULT_CHARS + 7)
    capped = cap_tool_output(text)
    assert capped.startswith("x" * MAX_TOOL_RESULT_CHARS + "\n\n[... 7 more characters omitted")
    assert capped.endswith("instead of paging through it in full.]")


def test_budget_reminder_names_the_size_with_thousands_separator() -> None:
    note = tool_output_budget_reminder(150_123)
    assert note.startswith("[System note: tool results in this run have grown to roughly 150,123 ")
    assert note.endswith("with no error message.]")


def _call(name: str = "search_records", **arguments: object) -> ToolCall:
    return ToolCall(id="c", name=name, arguments=dict(arguments))


def test_first_two_identical_calls_are_silent_and_the_third_nudges() -> None:
    state: dict[str, object] = {}
    state, r1 = track_repeat_tool_call(state, _call(model="a"))
    state, r2 = track_repeat_tool_call(state, _call(model="a"))
    state, r3 = track_repeat_tool_call(state, _call(model="a"))
    assert (r1, r2) == (None, None)
    assert r3 is not None and r3.startswith("You are repeating the exact same tool call")
    assert state == {"sig": 'search_records\n{"model": "a"}', "count": 3}


def test_fourth_is_silent_fifth_and_eighth_spell_out_the_call() -> None:
    state: dict[str, object] = {}
    reminders: list[str | None] = []
    for _ in range(8):
        state, r = track_repeat_tool_call(state, _call(model="a"))
        reminders.append(r)
    assert reminders[3] is None
    assert reminders[4] is not None and "'search_records' 5 times in a row" in reminders[4]
    assert reminders[6] is None
    assert reminders[7] is not None and "'search_records' 8 times in a row" in reminders[7]
    assert '{"model": "a"}' in reminders[7]


def test_a_different_call_resets_the_count() -> None:
    state: dict[str, object] = {}
    state, _ = track_repeat_tool_call(state, _call(model="a"))
    state, _ = track_repeat_tool_call(state, _call(model="a"))
    state, r = track_repeat_tool_call(state, _call(model="b"))
    assert r is None
    assert state["count"] == 1


def test_argument_order_does_not_break_a_repeat() -> None:
    state: dict[str, object] = {}
    state, _ = track_repeat_tool_call(state, ToolCall(id="1", name="t", arguments={"a": 1, "b": 2}))
    state, _ = track_repeat_tool_call(state, ToolCall(id="2", name="t", arguments={"b": 2, "a": 1}))
    state, r = track_repeat_tool_call(state, ToolCall(id="3", name="t", arguments={"a": 1, "b": 2}))
    assert r is not None


def test_a_long_argument_preview_is_truncated_with_an_ellipsis() -> None:
    state: dict[str, object] = {}
    big = "y" * 600
    r: str | None = None
    for _ in range(5):
        state, r = track_repeat_tool_call(state, _call(payload=big))
    assert r is not None
    assert "…" in r
    assert big not in r
