from oc8.agent.harness.stages.b_risk_tier import classify_tier


def test_scope_read_is_read() -> None:
    assert (
        classify_tier(
            "search_records",
            scopes={"read": ["search_records"], "modify": ["update_record"]},
            config={},
            annotations=None,
        )
        == "read"
    )


def test_unlisted_scope_is_write() -> None:
    assert classify_tier("mystery", scopes={"read": ["a"]}, config={}, annotations=None) == "write"


def test_destructive_hint_raises_read_to_destructive() -> None:
    assert (
        classify_tier(
            "search_records",
            scopes={"read": ["search_records"]},
            config={},
            annotations={"destructiveHint": True},
        )
        == "destructive"
    )


def test_plugin_outward_beats_destructive_hint() -> None:
    assert (
        classify_tier(
            "post_message",
            scopes={"modify": ["post_message"]},
            config={"outward_tools": ["post_message"], "destructive_tools": ["post_message"]},
            annotations={"readOnlyHint": True},
        )
        == "outward"
    )


def test_read_only_hint_does_not_raise_a_write_scope() -> None:
    # Spec: readOnlyHint → read only if scopes also allow it as read.
    assert (
        classify_tier(
            "update_record",
            scopes={"modify": ["update_record"]},
            config={},
            annotations={"readOnlyHint": True},
        )
        == "write"
    )


def test_control_tool_static_tier() -> None:
    assert classify_tier("ask_user", scopes=None, config=None, annotations=None) == "read"
    assert classify_tier("memory_write", scopes=None, config=None, annotations=None) == "write"


def test_outward_skip_spec_keeps_internal_note_as_write() -> None:
    """Odoo post_message subtype=note must not raise to outward (helpdesk_internal_note)."""
    cfg = {
        "outward_tools": ["post_message"],
        "outward_skip_spec": {"post_message": {"subtype": ["note"]}},
    }
    scopes = {"modify": ["post_message"]}
    assert (
        classify_tier(
            "post_message",
            scopes=scopes,
            config=cfg,
            annotations=None,
            arguments={"subtype": "note", "body": "Checked the logs"},
        )
        == "write"
    )
    assert (
        classify_tier(
            "post_message",
            scopes=scopes,
            config=cfg,
            annotations=None,
            arguments={"subtype": "comment", "body": "Hello"},
        )
        == "outward"
    )


def test_a_connection_tool_sharing_a_core_name_is_tiered_by_its_scopes() -> None:
    """`odoo.todo_write` is a write on odoo. Its bare name is the core to-do
    list, a read -- and the B gate let it through unasked."""
    assert (
        classify_tier(
            "todo_write",
            scopes={"modify": ["todo_write"]},
            config=None,
            annotations=None,
            core=False,
        )
        == "write"
    )
    assert classify_tier("todo_write", scopes=None, config=None, annotations=None) == "read"
