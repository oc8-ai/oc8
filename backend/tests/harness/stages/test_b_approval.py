"""B5 approval posture, previews, and justification handling."""

from __future__ import annotations

from oc8.agent.harness.stages.b_approval import (
    autonomy_of,
    posture,
    render_preview,
    strip_justification,
)


def test_strip_justification_copies_arguments_and_returns_text() -> None:
    original = {"id": 7, "justification": "Needed to fix the record"}
    arguments, justification = strip_justification(original)
    assert arguments == {"id": 7}
    assert justification == "Needed to fix the record"
    assert original == {"id": 7, "justification": "Needed to fix the record"}


def test_autonomy_defaults_and_rejects_unknown_values() -> None:
    assert autonomy_of(None) == "default"
    assert autonomy_of({}) == "default"
    assert autonomy_of({"autonomy": "unknown"}) == "default"
    assert autonomy_of({"autonomy": "conservative"}) == "conservative"
    assert autonomy_of({"autonomy": "autonomous"}) == "autonomous"


def test_posture_table() -> None:
    assert posture("read", "conservative", granted=False) == "allow"
    assert posture("write", "default", granted=False) == "allow"
    assert posture("write", "conservative", granted=False) == "ask"
    assert posture("destructive", "default", granted=False) == "ask"
    assert posture("irreversible", "autonomous", granted=False) == "allow"
    assert posture("outward", "autonomous", granted=False) == "ask"
    assert posture("outward", "autonomous", granted=True) == "allow"


def test_preview_uses_template_and_missing_keys_are_empty() -> None:
    assert (
        render_preview(
            tool="delete_record",
            connection="records",
            arguments={"id": 7},
            template="Delete {record} ({id}) for {missing}?",
            record="Customer #7",
        )
        == "Delete Customer #7 (7) for ?"
    )


def test_preview_falls_back_to_neutral_question() -> None:
    assert (
        render_preview(
            tool="change_record",
            connection="records",
            arguments={},
            template=None,
            record="Customer #7",
        )
        == "Allow records to run change_record on Customer #7?"
    )
