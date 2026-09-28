"""D3: verification reminders for records changed during the run."""

from __future__ import annotations

from oc8.agent.harness.stages.d_verify import (
    VERIFY_MAX_ROUNDS,
    verify_exhausted_note,
    verify_reminder,
)
from oc8.agent.harness.state import EntityRef


def test_verify_round_cap_is_two() -> None:
    assert VERIFY_MAX_ROUNDS == 2


def test_reminder_describes_one_changed_record() -> None:
    entry = EntityRef(
        connection="c",
        kind="order",
        id="9",
        label="Order",
        last_write_step=2,
        write_tools=["update_record"],
    )
    assert verify_reminder([entry]) == (
        "Before finishing, verify your changes against the system. Re-read these "
        "records and confirm the fields you changed hold the intended values:\n"
        "- Order (order 9) — changed by update_record at step 2\n"
        "Report what you verified. If a value is wrong, fix it now."
    )


def test_reminder_describes_two_records_with_fallbacks() -> None:
    entries = [
        EntityRef(
            connection="c",
            kind="order",
            id="9",
            label="Order",
            last_write_step=2,
            write_tools=["create_record", "update_record"],
        ),
        EntityRef(connection="c", kind="invoice", id="4"),
    ]
    text = verify_reminder(entries)
    assert (
        "- Order (order 9) — changed by update_record at step 2\n"
        "- invoice (invoice 4) — changed by a write at step ?"
    ) in text


def test_exhausted_note_lists_one_record() -> None:
    entry = EntityRef(connection="c", kind="order", id="9", label="Order")
    assert verify_exhausted_note([entry]) == (
        "[Note: 1 change(s) were not re-verified: Order (order 9)]"
    )


def test_exhausted_note_lists_two_records_with_label_fallback() -> None:
    entries = [
        EntityRef(connection="c", kind="order", id="9", label="Order"),
        EntityRef(connection="c", kind="invoice", id="4"),
    ]
    assert verify_exhausted_note(entries) == (
        "[Note: 2 change(s) were not re-verified: "
        "Order (order 9), invoice (invoice 4)]"
    )
