from oc8.agent.harness.stages.c_ledger import (
    ledger_fingerprint,
    record_decision,
    record_file,
    record_outward,
    render_ledger_block,
)
from oc8.agent.harness.state import EntityRef, Ledger


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


def test_empty_ledger_renders_four_none_lines() -> None:
    assert render_ledger_block(Ledger()) == (
        "# Working state (supersedes earlier working-state blocks)\n"
        "Records touched:\n"
        "- none\n"
        "Messages sent:\n"
        "- none\n"
        "Decisions requested:\n"
        "- none\n"
        "Files produced:\n"
        "- none"
    )


def test_written_unverified_entity_says_it_was_not_re_read() -> None:
    key = "mail/message/42"
    ledger = Ledger(
        entities={
            key: EntityRef(
                connection="mail",
                kind="message",
                id="42",
                label="Quarterly update",
                first_read_step=1,
                last_read_step=1,
                last_write_step=3,
                write_tools=["update_message"],
            )
        },
        writes_unverified=[key],
    )

    assert (
        '- mail/message/42 "Quarterly update" — read step 1, '
        "written step 3 (update_message), not re-read since"
    ) in render_ledger_block(ledger)


def test_read_only_entity_omits_written_clause() -> None:
    ledger = Ledger(
        entities={
            "mail/message/42": EntityRef(
                connection="mail",
                kind="message",
                id="42",
                label="Quarterly update",
                first_read_step=2,
                last_read_step=4,
            )
        }
    )

    block = render_ledger_block(ledger)
    assert '- mail/message/42 "Quarterly update" — read step 4' in block
    assert "written step" not in block


def test_fingerprint_is_stable_and_changes_with_a_new_file() -> None:
    ledger = Ledger()
    first = ledger_fingerprint(ledger)
    assert ledger_fingerprint(ledger) == first

    ledger.files.append("summary.txt")
    assert ledger_fingerprint(ledger) != first
