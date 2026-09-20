"""The Harness object owns the per-run state and applies C3 (step stamp), C5
(shape) and D1 (may_finish) exactly as the two runtimes do today, in today's
order: the step stamp first, then the tool message, then the repeat nudge,
then the budget note."""

from __future__ import annotations

import datetime as dt

from oc8.agent.harness import FinishVerdict, Harness, HarnessState, ModelCaps
from oc8.agent.harness.prompts import format_step_stamp, resolve_timezone
from oc8.agent.harness.stages.c_reminders import (
    MAX_TOOL_RESULT_CHARS,
    TOOL_OUTPUT_BUDGET_WARNING_CHARS,
)
from oc8.modelrouter import ToolCall


def _call(**arguments: object) -> ToolCall:
    return ToolCall(id="c", name="search_records", arguments=dict(arguments))


def _stamp_prefix_len(step_no: int, max_steps: int, tz: str = "UTC") -> int:
    """The exact character count `shape()` prepends before the raw/capped
    output, via the real `format_step_stamp` plus the one joining space. The
    clock reading itself doesn't matter here -- `HH:MM` is always 5 characters
    regardless of the actual time, so a fixed `now` gives the real length."""
    tz_label, resolved_tz = resolve_timezone(tz)
    now = dt.datetime(2020, 1, 1, tzinfo=resolved_tz)
    stamp = format_step_stamp(step_no=step_no, max_steps=max_steps, now=now, tz_label=tz_label)
    return len(stamp) + 1


def test_shape_prepends_step_stamp() -> None:
    h = Harness(caps=ModelCaps())
    h.state.step_no = 3
    shaped = h.shape(_call(model="a"), "ok", max_steps=40, tz="UTC")
    assert shaped.output.startswith("[step 3/40 · ")
    assert shaped.output.endswith("] ok")


def test_step_stamp_uses_given_timezone() -> None:
    h = Harness(caps=ModelCaps())
    h.state.step_no = 1
    shaped = h.shape(_call(model="a"), "ok", max_steps=10, tz="Europe/Berlin")
    assert "Europe/Berlin" in shaped.output


def test_a_small_result_passes_through_with_no_reminders() -> None:
    h = Harness()
    shaped = h.shape(_call(model="a"), "ok", max_steps=40, tz="UTC")
    assert shaped.output.startswith("[step 0/40 · ")
    assert shaped.output.endswith("] ok")
    assert shaped.reminders == []
    assert h.state.tool_output_chars == _stamp_prefix_len(0, 40) + len("ok")
    assert h.state.repeat == {"sig": 'search_records\n{"model": "a"}', "count": 1}


def test_an_oversized_result_is_capped_and_the_capped_size_is_what_counts() -> None:
    h = Harness()
    shaped = h.shape(_call(), "x" * (MAX_TOOL_RESULT_CHARS + 100), max_steps=40, tz="UTC")
    # cap_tool_output adds a note (~200+ chars) and the step stamp adds its own
    # prefix, so allow for both on top of the cap.
    assert len(shaped.output) < MAX_TOOL_RESULT_CHARS + 300
    assert h.state.tool_output_chars == len(shaped.output)


def test_budget_note_fires_once_when_the_cumulative_size_crosses_the_line() -> None:
    h = Harness()
    # step_no/max_steps/tz never change across the loop below, so the stamp's
    # length is constant -- size the chunk so each stamped-and-capped result
    # is exactly MAX_TOOL_RESULT_CHARS, matching the pre-stamp behavior this
    # test's numbers were chosen for.
    prefix_len = _stamp_prefix_len(h.state.step_no, 40, "UTC")
    chunk = "x" * (MAX_TOOL_RESULT_CHARS - prefix_len)
    notes: list[list[str]] = []
    for i in range(10):
        notes.append(h.shape(_call(i=i), chunk, max_steps=40, tz="UTC").reminders)
    first_note_at = next(i for i, r in enumerate(notes) if r)
    # 20_000 * 8 = 160_000 >= 150_000 -> the 8th result (index 7) trips it.
    assert first_note_at == 7
    assert h.state.tool_output_budget_warned is True
    assert TOOL_OUTPUT_BUDGET_WARNING_CHARS <= 8 * MAX_TOOL_RESULT_CHARS
    assert all(not r for r in notes[8:]), "the budget note is issued once per run"


def test_repeat_nudge_comes_before_the_budget_note() -> None:
    # Each "a" result adds prefix_len + 1 chars now that shape() stamps it
    # first; pick starting totals so the crossing points land on the same
    # calls as before the stamp existed.
    increment = _stamp_prefix_len(0, 40, "UTC") + 1

    h = Harness(state=HarnessState(tool_output_chars=TOOL_OUTPUT_BUDGET_WARNING_CHARS - increment))
    h.shape(_call(), "a", max_steps=40, tz="UTC")
    h.shape(_call(), "a", max_steps=40, tz="UTC")
    # The third identical call crosses the repeat threshold; the budget line was
    # crossed on the first call already, so only the repeat nudge is new here.
    shaped = h.shape(_call(), "a", max_steps=40, tz="UTC")
    assert len(shaped.reminders) == 1
    assert shaped.reminders[0].startswith("You are repeating")

    h2 = Harness(
        state=HarnessState(tool_output_chars=TOOL_OUTPUT_BUDGET_WARNING_CHARS - 3 * increment)
    )
    h2.shape(_call(), "a", max_steps=40, tz="UTC")
    h2.shape(_call(), "a", max_steps=40, tz="UTC")
    shaped2 = h2.shape(_call(), "a", max_steps=40, tz="UTC")
    assert [r[:20] for r in shaped2.reminders] == [
        "You are repeating th",
        "[System note: tool r",
    ]


def test_may_finish_nudges_three_times_then_lets_the_run_end_with_a_note() -> None:
    h = Harness()
    open_todos = [{"content": "B", "status": "pending"}]
    verdicts = [h.may_finish(open_todos) for _ in range(4)]
    assert [v.ok for v in verdicts] == [False, False, False, True]
    assert verdicts[0].reminder is not None and "round 1/3" in verdicts[0].reminder
    assert verdicts[2].reminder is not None and "round 3/3" in verdicts[2].reminder
    assert verdicts[3].reminder is None
    assert (
        verdicts[3].exhausted_note is not None
        and "1 todo item(s) still open" in verdicts[3].exhausted_note
    )
    assert h.state.todo_rounds == 3


def test_may_finish_with_nothing_open_is_a_clean_finish() -> None:
    assert Harness().may_finish([]) == FinishVerdict(ok=True)


def test_may_finish_does_not_nudge_when_the_caller_cannot_continue() -> None:
    h = Harness()
    v = h.may_finish([{"content": "B", "status": "pending"}], can_continue=False)
    assert v.ok is True and v.exhausted_note is not None
    assert h.state.todo_rounds == 0


def test_from_run_context_and_store_round_trip() -> None:
    ctx: dict[str, object] = {"task": "x"}
    h = Harness.from_run_context(ctx, caps=ModelCaps(code_mode=True))
    h.shape(_call(), "abc", max_steps=40, tz="UTC")
    h.store(ctx)
    again = Harness.from_run_context(ctx)
    assert again.state.tool_output_chars == _stamp_prefix_len(0, 40) + len("abc")
    assert h.caps.code_mode is True
    assert again.caps == ModelCaps()
