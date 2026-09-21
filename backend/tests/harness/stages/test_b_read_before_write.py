from oc8.agent.harness.stages.b_read_before_write import (
    entity_key,
    note_access,
    read_before_write_denial,
)
from oc8.agent.harness.state import Ledger


def test_entity_key_joins_connection_kind_and_id() -> None:
    assert entity_key("office", "document", "42") == "office/document/42"


def test_read_before_write_can_be_disabled() -> None:
    assert (
        read_before_write_denial(
            tool="update_document",
            tier="write",
            identity=("document", "42"),
            ledger=Ledger(),
            connection="office",
            config={"read_before_write": False},
            label="Quarterly plan",
        )
        is None
    )


def test_create_without_an_entity_identity_is_exempt() -> None:
    assert (
        read_before_write_denial(
            tool="create_document",
            tier="write",
            identity=None,
            ledger=Ledger(),
            connection="office",
            config={},
            label="Quarterly plan",
        )
        is None
    )


def test_read_tool_is_exempt() -> None:
    assert (
        read_before_write_denial(
            tool="get_document",
            tier="read",
            identity=("document", "42"),
            ledger=Ledger(),
            connection="office",
            config={},
            label="Quarterly plan",
        )
        is None
    )


def test_write_is_denied_until_entity_was_read() -> None:
    assert read_before_write_denial(
        tool="update_document",
        tier="write",
        identity=("document", "42"),
        ledger=Ledger(),
        connection="office",
        config={"entity_lookup_tools": ["find_document", "get_document"]},
        label="Quarterly plan",
    ) == (
        "Precondition not met: you have not read Quarterly plan (document 42) in this run.\n"
        "Read it first — e.g. find_document —\n"
        "then make the change. This is a policy decision, not a tool error."
    )


def test_denial_uses_generic_suggestion_without_lookup_tool() -> None:
    assert read_before_write_denial(
        tool="update_document",
        tier="write",
        identity=("document", "42"),
        ledger=Ledger(),
        connection="office",
        config=None,
        label="Quarterly plan",
    ) == (
        "Precondition not met: you have not read Quarterly plan (document 42) in this run.\n"
        "Read it first — e.g. a search/read tool —\n"
        "then make the change. This is a policy decision, not a tool error."
    )


def test_note_access_allows_a_later_write() -> None:
    ledger = Ledger()
    note_access(
        ledger,
        connection="office",
        kind="document",
        id="42",
        label="Quarterly plan",
        step_no=3,
        wrote=False,
        tool="get_document",
    )

    assert (
        read_before_write_denial(
            tool="update_document",
            tier="write",
            identity=("document", "42"),
            ledger=ledger,
            connection="office",
            config={},
            label="Quarterly plan",
        )
        is None
    )
    entity = ledger.entities["office/document/42"]
    assert entity.first_read_step == 3
    assert entity.last_read_step == 3
    assert entity.last_write_step is None
    assert entity.write_tools == []


def test_note_access_tracks_writes_without_marking_a_read() -> None:
    ledger = Ledger()
    note_access(
        ledger,
        connection="office",
        kind="document",
        id="42",
        label="Quarterly plan",
        step_no=4,
        wrote=True,
        tool="update_document",
    )

    entity = ledger.entities["office/document/42"]
    assert entity.first_read_step is None
    assert entity.last_read_step is None
    assert entity.last_write_step == 4
    assert entity.write_tools == ["update_document"]
