"""D3 -- verify records changed during the run before finishing."""

from __future__ import annotations

from oc8.agent.harness.state import EntityRef

VERIFY_MAX_ROUNDS = 2


def _entry_label(entry: EntityRef) -> str:
    return f"{entry.label or entry.kind} ({entry.kind} {entry.id})"


def verify_reminder(entries: list[EntityRef]) -> str:
    """Ask the model to re-read records whose latest write is unverified."""
    lines = "\n".join(
        f"- {_entry_label(entry)} — changed by "
        f"{entry.write_tools[-1] if entry.write_tools else 'a write'} at step "
        f"{entry.last_write_step if entry.last_write_step is not None else '?'}"
        for entry in entries
    )
    return (
        "Before finishing, verify your changes against the system. Re-read these "
        "records and confirm the fields you changed hold the intended values:\n"
        f"{lines}\n"
        "Report what you verified. If a value is wrong, fix it now."
    )


def verify_exhausted_note(entries: list[EntityRef]) -> str:
    """Describe writes that remained unverified when the run ended."""
    labels = ", ".join(_entry_label(entry) for entry in entries)
    return f"[Note: {len(entries)} change(s) were not re-verified: {labels}]"
