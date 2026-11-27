"""A model's call ids are not unique (security scan): one reused for a
different call must not ride on what was booked against the first."""

from __future__ import annotations

from oc8.agent.harness.calls import unique_call_ids
from oc8.modelrouter import ToolCall


def test_a_reused_id_is_renamed_and_the_first_keeps_its_own() -> None:
    calls = [
        ToolCall(id="c1", name="a", arguments={}),
        ToolCall(id="c1", name="b", arguments={}),
        ToolCall(id="c1", name="c", arguments={}),
        ToolCall(id="", name="d", arguments={}),
    ]
    taken: set[str] = set()
    unique_call_ids(calls, taken)
    assert [c.id for c in calls] == ["c1", "c1_dup1", "c1_dup2", "call_dup1"]
    assert taken == {"c1", "c1_dup1", "c1_dup2", "call_dup1"}


def test_an_id_used_earlier_in_the_run_is_renamed() -> None:
    later = [ToolCall(id="c1", name="a", arguments={})]
    unique_call_ids(later, {"c1", "c1_dup1"})
    assert later[0].id == "c1_dup2"
