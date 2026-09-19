"""HarnessState round-trips through run.context (isolated runtime) and starts
empty in memory (in-process). A run that started before package 1 still
carries its counters as three top-level context keys; loading must read those
once so an in-flight run does not lose its budget/repeat state mid-run."""

from __future__ import annotations

import json

from oc8.agent.harness.calls import call_sig
from oc8.agent.harness.state import CONTEXT_KEY, HarnessState
from oc8.modelrouter import ToolCall


def test_fresh_state_is_all_zero() -> None:
    s = HarnessState()
    assert s.version == 1
    assert s.tool_output_chars == 0
    assert s.tool_output_budget_warned is False
    assert s.repeat == {}
    assert s.todo_rounds == 0


def test_round_trip_through_real_json() -> None:
    s = HarnessState(
        tool_output_chars=1234,
        tool_output_budget_warned=True,
        repeat={"sig": "search_records\n{}", "count": 2},
        todo_rounds=1,
    )
    raw = json.loads(json.dumps(s.to_dict()))
    assert HarnessState.from_dict(raw) == s


def test_from_dict_tolerates_missing_and_none() -> None:
    assert HarnessState.from_dict(None) == HarnessState()
    assert HarnessState.from_dict({}) == HarnessState()
    assert HarnessState.from_dict({"version": 1}) == HarnessState()


def test_legacy_context_keys_are_read_once() -> None:
    ctx = {
        "task": "x",
        "tool_output_chars": 500,
        "tool_output_budget_warned": True,
        "repeat_tracker": {"sig": "a\n{}", "count": 3},
    }
    s = HarnessState.from_run_context(ctx)
    assert s.tool_output_chars == 500
    assert s.tool_output_budget_warned is True
    assert s.repeat == {"sig": "a\n{}", "count": 3}


def test_store_writes_the_harness_key_and_drops_legacy_keys() -> None:
    ctx = {"task": "x", "tool_output_chars": 500, "repeat_tracker": {"sig": "a", "count": 1}}
    s = HarnessState.from_run_context(ctx)
    s.tool_output_chars += 1
    s.store(ctx)
    assert ctx[CONTEXT_KEY]["tool_output_chars"] == 501  # type: ignore[index]
    assert "tool_output_chars" not in ctx
    assert "repeat_tracker" not in ctx
    assert "tool_output_budget_warned" not in ctx
    assert ctx["task"] == "x"
    # And the harness key wins over any legacy leftovers on the next load.
    assert HarnessState.from_run_context(ctx) == s


def test_call_sig_is_stable_across_argument_order() -> None:
    a = ToolCall(id="1", name="t", arguments={"x": 1, "y": [2]})
    b = ToolCall(id="2", name="t", arguments={"y": [2], "x": 1})
    assert call_sig(a) == call_sig(b) == 't\n{"x": 1, "y": [2]}'
