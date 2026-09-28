"""The per-call state derivation shared by both runtimes (run step timeline
plan, Task 1 -- addendum-scoped).

Only `call_state_for`/`CALL_STATES`/`REQUIRED_CALL_KEYS` are built here: a
pre-flight investigation found dev already shipped step-level timing capture
(`oc8.agent.harness.step_timing`), wired into both runtimes, so this plan does
not rebuild that. See `tests/agents/test_engine_step_timings.py` for the
verification test proving that existing capture already satisfies this plan's
needs.

Pure unit tests: no DB, no app.
"""

from __future__ import annotations

from oc8.runtime.step_record import CALL_STATES, REQUIRED_CALL_KEYS, call_state_for


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
