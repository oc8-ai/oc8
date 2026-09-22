from oc8.agent.harness.stages.c_ledger import (
    record_decision,
    record_file,
    record_outward,
)
from oc8.agent.harness.state import Ledger


def test_outward_file_and_decision_append_once() -> None:
    ledger = Ledger()
    record_outward(
        ledger,
        connection="mail",
        tool="send_note",
        target="ada@example.com",
        step=4,
    )
    record_outward(
        ledger,
        connection="mail",
        tool="send_note",
        target="ada@example.com",
        step=4,
    )
    record_file(ledger, "step-4-send_note.txt")
    record_file(ledger, "step-4-send_note.txt")
    record_decision(ledger, tool="ask_user", question="Which one?", step=2)
    assert len(ledger.outward) == 1
    assert ledger.files == ["step-4-send_note.txt"]
    assert ledger.decisions[0].question == "Which one?"


def test_recorders_truncate_text_and_skip_empty_filenames() -> None:
    ledger = Ledger()
    record_outward(ledger, connection="mail", tool="send_note", target="x" * 201, step=1)
    record_file(ledger, "")
    record_decision(ledger, tool="request_decision", question="q" * 201, step=1)

    assert ledger.outward[0].target == "x" * 200
    assert ledger.files == []
    assert ledger.decisions[0].question == "q" * 200
