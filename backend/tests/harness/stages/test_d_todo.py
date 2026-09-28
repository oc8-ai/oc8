"""D1: the todo-continuation texts, frozen as they are today."""

from __future__ import annotations

from oc8.agent.harness.stages.d_todo import (
    TODO_CONTINUATION_MAX_ROUNDS,
    todo_continuation_exhausted_note,
    todo_continuation_reminder,
)

_OPEN = [
    {"content": "Resolve ticket B", "status": "pending"},
    {"content": "Answer the customer", "status": "in_progress"},
]


def test_round_cap_is_three() -> None:
    assert TODO_CONTINUATION_MAX_ROUNDS == 3


def test_reminder_lists_every_open_item_with_its_status_and_the_round() -> None:
    text = todo_continuation_reminder(_OPEN, 2)
    assert text.startswith(
        "You indicated you are finished, but 2 todo item(s) from your own todo_write list "
        "are still open (continuation round 2/3):\n"
    )
    assert "- [pending] Resolve ticket B\n- [in_progress] Answer the customer\n" in text
    assert text.endswith("explain why before finishing.")


def test_reminder_defaults_a_missing_status_to_pending() -> None:
    assert "- [pending] x" in todo_continuation_reminder([{"content": "x"}], 1)


def test_exhausted_note_is_bracketed_and_counts_rounds() -> None:
    note = todo_continuation_exhausted_note(_OPEN)
    assert note.startswith(
        "[Note: this run ended with 2 todo item(s) still open after 3 continuation attempt(s):\n"
    )
    assert note.endswith("- [in_progress] Answer the customer]")
