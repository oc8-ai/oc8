"""One approval covers exactly one run of the call it approved (E2E N1,
security review). `Decisions` is what every dispatch path reads."""

from __future__ import annotations

from oc8.runtime.approval_resume import Decisions


def _approve(sig: str, approval_id: str | None = None) -> dict[str, str]:
    entry = {"sig": sig, "decision": "approve"}
    if approval_id is not None:
        entry["approval_id"] = approval_id
    return entry


def test_an_approval_its_call_used_up_approves_nothing_more() -> None:
    ctx = {
        "resolved_tool_approvals": [_approve("s", "a1")],
        "consumed_tool_approvals": [{"sig": "s", "key": "a1"}],
    }
    decisions = Decisions(ctx)
    assert decisions.get("s") is None
    assert not decisions.approved("s")
    assert decisions  # still a resume leg: somebody decided


def test_two_approvals_of_the_same_call_cover_two_runs() -> None:
    ctx = {"resolved_tool_approvals": [_approve("s", "a1"), _approve("s", "a2")]}
    assert Decisions(ctx).open["s"] == "a1"
    used = {**ctx, "consumed_tool_approvals": [{"sig": "s", "key": "a1"}]}
    assert Decisions(used).open["s"] == "a2"
    both = {**ctx, "consumed_tool_approvals": [{"sig": "s", "key": k} for k in ("a1", "a2")]}
    assert Decisions(both).get("s") is None


def test_an_approval_without_an_id_is_counted() -> None:
    ctx = {
        "resolved_tool_approvals": [_approve("s"), _approve("s")],
        "consumed_tool_approvals": [{"sig": "s", "key": "s#1"}],
    }
    assert Decisions(ctx).open["s"] == "s#2"


def test_a_rejection_is_never_used_up() -> None:
    ctx = {
        "resolved_tool_approvals": [{"sig": "s", "decision": "reject"}],
        "consumed_tool_approvals": [{"sig": "s", "key": "s#1"}],
    }
    assert Decisions(ctx).get("s") == "reject"


def test_dropping_forgets_the_approval_in_this_process() -> None:
    decisions = Decisions({"resolved_tool_approvals": [_approve("s", "a1")]})
    assert decisions.drop("s") == "a1"
    assert decisions.get("s") is None
    assert decisions.drop("s") is None


def test_a_plain_map_reads_as_unused_decisions() -> None:
    decisions = Decisions.of({"s": "approve", "t": "reject"})
    assert decisions is not None
    assert (decisions.get("s"), decisions.get("t")) == ("approve", "reject")
