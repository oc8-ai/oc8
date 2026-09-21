from oc8.agent.harness.stages.c_errors import ToolError, render_error


def test_mcp_error_names_the_connection_and_tells_the_model_not_to_repeat() -> None:
    text = render_error(
        "search_records",
        "odoo",
        ToolError(kind="mcp", message="Invalid field 'sla_date'"),
    )
    assert text.startswith("ERROR from odoo (search_records): Invalid field 'sla_date'")
    assert "do not repeat the identical call" in text


def test_timeout_adds_duration_and_the_partial_execution_warning() -> None:
    text = render_error(
        "search_records",
        "odoo",
        ToolError(kind="timeout", message="timed out", duration_s=180.0),
    )
    assert "ERROR from odoo (search_records): timed out" in text
    assert "180" in text
    assert "may have partially executed" in text


def test_deny_and_control_use_the_oc8_source() -> None:
    deny = render_error(
        "send_mail",
        "oc8",
        ToolError(kind="deny", message="operator rejected this action"),
    )
    assert deny == "ERROR from oc8 (send_mail): operator rejected this action"
    ctrl = render_error(
        "read_run_file",
        "oc8",
        ToolError(kind="control", message="no such file: x.txt"),
    )
    assert ctrl == "ERROR from oc8 (read_run_file): no such file: x.txt"


def test_original_message_is_never_dropped() -> None:
    msg = "something unique 7f3a"
    text = render_error("t", "oc8", ToolError(kind="control", message=msg))
    assert msg in text
