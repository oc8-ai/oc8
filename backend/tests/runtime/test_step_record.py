"""The per-call state derivation shared by both runtimes (run step timeline
plan, Task 1 -- addendum-scoped).

Only `call_state_for`/`CALL_STATES`/`REQUIRED_CALL_KEYS` are built here: a
pre-flight investigation found dev already shipped step-level timing capture
(`oc8.agent.harness.step_timing`), wired into both runtimes, so this plan does
not rebuild that. See `tests/agents/test_engine_step_timings.py` for the
verification test proving that existing capture already satisfies this plan's
needs.

`step_timing_dto` (Task 6) is also covered here: the one small thing this
module owns about that timing data, translating a stored snake_case entry
into the camelCase shape both `run_to_dto` and every `publish_run_step_timing`
call site commit to on the wire.

Pure unit tests: no DB, no app.
"""

from __future__ import annotations

from oc8.runtime.step_record import (
    CALL_STATES,
    REQUIRED_CALL_KEYS,
    call_state_for,
    step_timing_dto,
)


def test_a_dispatched_call_with_a_plain_result_is_done() -> None:
    assert call_state_for("created id=7", dispatched=True) == "done"


def test_a_dispatched_call_that_errored_failed() -> None:
    assert call_state_for("ERROR: connection refused", dispatched=True) == "failed"


def test_an_undispatched_call_is_denied_even_though_its_output_reads_as_an_error() -> None:
    """The DENY branch writes `ERROR: <reason>` as the output the model sees,
    so `dispatched` -- not the text -- is what tells a refusal apart from a
    failure. Without this a guardrail denial would render as a red failed
    step and read like the tool broke."""
    assert call_state_for("ERROR: tool not allowed by the frame", dispatched=False) == "denied"


def test_every_state_call_state_for_can_return_is_a_known_state() -> None:
    produced = {
        call_state_for("ok", dispatched=True),
        call_state_for("ERROR: x", dispatched=True),
        call_state_for("ERROR: x", dispatched=False),
    }
    assert produced <= CALL_STATES


def test_the_required_call_keys_are_the_five_both_runtimes_must_write() -> None:
    assert REQUIRED_CALL_KEYS == {"tool", "arguments", "step", "connection", "state"}


def test_step_timing_dto_translates_the_stored_snake_case_shape_to_camel_case() -> None:
    stored = {
        "step": 3,
        "model_wait_ms": 120,
        "ttft_ms": 40,
        "tool_wait_ms": 5,
        "step_wall_ms": 200,
    }
    assert step_timing_dto(stored) == {
        "step": 3,
        "modelWaitMs": 120,
        "ttftMs": 40,
        "toolWaitMs": 5,
        "stepWallMs": 200,
    }


def test_step_timing_dto_carries_a_missing_ttft_through_as_none() -> None:
    """A step that never streamed a first token (no model call reached, or a
    non-streaming completion) leaves `ttft_ms` unset -- the timeline must
    render that as a missing duration, not a coerced zero."""
    stored = {"step": 1, "model_wait_ms": 10, "tool_wait_ms": 0, "step_wall_ms": 15}
    assert step_timing_dto(stored)["ttftMs"] is None
