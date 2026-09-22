from oc8.agent.harness.stages.a_masking import mask_observations
from oc8.agent.harness.state import EntityRef, Ledger
from oc8.modelrouter import NeutralMessage


def _tool_result(body: str, *, step: int = 1, tool: str = "lookup") -> NeutralMessage:
    return NeutralMessage(
        role="tool",
        content=f"[step {step}/200 · 00:00 UTC] {body}",
        tool_call_id=f"call-{step}",
        name=tool,
    )


def test_old_long_result_is_masked_without_mutating_input() -> None:
    original = _tool_result("x" * 2_500)
    messages = [original]

    outgoing, masked = mask_observations(messages, step_no=10, ledger=Ledger())

    assert outgoing is not messages
    assert outgoing[0] is not original
    assert original.content.endswith("x" * 2_500)
    assert len(outgoing[0].content) < len(original.content)
    assert "x" * 400 in outgoing[0].content
    assert masked["call-1"].step == 1
    assert masked["call-1"].tool == "lookup"
    assert masked["call-1"].chars == 2_500


def test_recent_long_result_is_kept() -> None:
    original = _tool_result("x" * 2_500)

    outgoing, masked = mask_observations([original], step_no=6, ledger=Ledger())

    assert outgoing == [original]
    assert masked == {}


def test_age_exactly_six_masks() -> None:
    original = _tool_result("x" * 2_500)

    outgoing, masked = mask_observations([original], step_no=7, ledger=Ledger())

    assert outgoing != [original]
    assert masked["call-1"].step == 1
    assert len(outgoing[0].content) < len(original.content)


def test_exactly_8000_chars_has_no_spill_pointer() -> None:
    outgoing, masked = mask_observations(
        [_tool_result("x" * 8_000)], step_no=10, ledger=Ledger()
    )

    assert "read_run_file" not in outgoing[0].content
    assert masked["call-1"].filename is None


def test_old_short_result_is_kept() -> None:
    original = _tool_result("x" * 1_500)

    outgoing, masked = mask_observations([original], step_no=10, ledger=Ledger())

    assert outgoing == [original]
    assert masked == {}


def test_spill_pointer_is_only_included_above_8000_chars() -> None:
    long_outgoing, _ = mask_observations(
        [_tool_result("x" * 8_001)], step_no=10, ledger=Ledger()
    )
    short_outgoing, _ = mask_observations(
        [_tool_result("x" * 2_500)], step_no=10, ledger=Ledger()
    )

    assert 'read_run_file("step-1-lookup.txt")' in long_outgoing[0].content
    assert "read_run_file" not in short_outgoing[0].content


def test_latest_read_of_unverified_write_is_kept() -> None:
    key = "office/document/42"
    original = _tool_result("x" * 2_500, step=1)
    ledger = Ledger(
        entities={
            key: EntityRef(
                connection="office",
                kind="document",
                id="42",
                last_read_step=1,
                last_write_step=3,
            )
        },
        writes_unverified=[key],
    )

    outgoing, masked = mask_observations([original], step_no=10, ledger=ledger)

    assert outgoing == [original]
    assert masked == {}


def test_unstamped_result_is_kept() -> None:
    original = NeutralMessage(
        role="tool",
        content="x" * 2_500,
        tool_call_id="call-unstamped",
        name="lookup",
    )

    outgoing, masked = mask_observations([original], step_no=10, ledger=Ledger())

    assert outgoing == [original]
    assert masked == {}
