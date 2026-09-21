from oc8.agent.harness.stages.c_spill import (
    SPILL_HEAD_CHARS,
    SPILL_TAIL_CHARS,
    SPILL_THRESHOLD_CHARS,
    maybe_spill,
    spill_filename,
)


def test_threshold_and_window_constants() -> None:
    assert SPILL_THRESHOLD_CHARS == 8_000
    assert SPILL_HEAD_CHARS == 3_000
    assert SPILL_TAIL_CHARS == 800


def test_filename_sanitises_the_tool_name() -> None:
    assert spill_filename(3, "search_records") == "step-3-search_records.txt"
    assert spill_filename(12, "odoo/search records") == "step-12-odoo_search_records.txt"


def test_a_short_body_is_returned_untouched() -> None:
    body = "ok"
    text, spill = maybe_spill("search_records", body, step_no=1)
    assert text is body
    assert spill is None


def test_a_body_at_the_threshold_is_untouched() -> None:
    body = "x" * SPILL_THRESHOLD_CHARS
    text, spill = maybe_spill("search_records", body, step_no=1)
    assert text is body
    assert spill is None


def test_an_oversized_body_becomes_a_preview_and_a_spill() -> None:
    middle = SPILL_THRESHOLD_CHARS - SPILL_HEAD_CHARS - SPILL_TAIL_CHARS + 50
    body = "H" * SPILL_HEAD_CHARS + "M" * middle + "T" * SPILL_TAIL_CHARS
    assert len(body) == SPILL_THRESHOLD_CHARS + 50
    text, spill = maybe_spill("search_records", body, step_no=4)
    assert spill is not None
    assert spill.filename == "step-4-search_records.txt"
    assert spill.content == body
    assert text.startswith("[search_records returned ")
    assert f'kept as file "{spill.filename}"' in text
    assert "Use read_run_file(" in text
    assert text.endswith("T" * SPILL_TAIL_CHARS)
    omitted = len(body) - SPILL_HEAD_CHARS - SPILL_TAIL_CHARS
    assert f"… {omitted} chars omitted …" in text
    assert "M" * middle not in text
