"""Where a run stops besides its step budget: the tokens it used, and a call
it keeps repeating without getting anywhere.

A scheduled run took 52 steps at ~21,000 tokens each to report "I found a
few calendar entries that might be about travel" -- nothing stopped it but
the step budget of 200. Both runtimes ask these two functions once per step,
so they stop a run for the same reasons."""

from __future__ import annotations

import pytest

from oc8.agent.harness.stages.c_reminders import REPEAT_CALL_THRESHOLDS
from oc8.agent.run_limits import (
    NO_PROGRESS_OUTPUT,
    REPEAT_LIMIT,
    TOKEN_LIMIT_OUTPUT,
    max_run_tokens,
    repeated_call,
    run_tokens,
    stop_reason,
)
from oc8.config import get_settings


def test_tokens_add_up_over_every_step_that_recorded_them() -> None:
    steps = [
        {"step": 1, "tokens_in": 20_000, "tokens_out": 800},
        {"step": 2},
        {"step": 3, "tokens_in": 21_000, "tokens_out": 200},
    ]
    assert run_tokens(steps) == 42_000


def test_an_agent_sets_its_own_token_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_run_tokens", 0)
    assert max_run_tokens({"max_run_tokens": 100_000}) == 100_000
    assert max_run_tokens({}) is None
    monkeypatch.setattr(get_settings(), "agent_max_run_tokens", 500_000)
    assert max_run_tokens({}) == 500_000
    assert max_run_tokens({"max_run_tokens": 0}) == 500_000


def test_identical_calls_stop_only_after_every_repeat_reminder_had_its_chance() -> None:
    assert REPEAT_LIMIT > REPEAT_CALL_THRESHOLDS[-1]
    call = {"tool": "odoo_search", "arguments": {"model": "calendar.event", "limit": 80}}
    assert repeated_call([call] * (REPEAT_LIMIT - 1)) is None
    assert repeated_call([call] * REPEAT_LIMIT) == "odoo_search"
    assert repeated_call([{"tool": "x", "arguments": {}}] + [call] * (REPEAT_LIMIT - 1)) is None


def test_different_arguments_are_progress() -> None:
    calls = [{"tool": "odoo_search", "arguments": {"offset": n * 80}} for n in range(REPEAT_LIMIT)]
    assert repeated_call(calls) is None


def test_arguments_compare_whatever_their_key_order() -> None:
    a = {"tool": "t", "arguments": {"x": 1, "y": 2}}
    b = {"tool": "t", "arguments": {"y": 2, "x": 1}}
    assert repeated_call([a, b] * REPEAT_LIMIT) == "t"


def test_stop_reason_names_the_limit_it_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_run_tokens", 0)
    steps = [{"tokens_in": 60_000, "tokens_out": 0}]
    assert stop_reason({"max_run_tokens": 50_000}, steps, []) == TOKEN_LIMIT_OUTPUT
    call = {"tool": "t", "arguments": {}}
    assert stop_reason({}, [], [call] * REPEAT_LIMIT) == NO_PROGRESS_OUTPUT
    assert stop_reason({"max_run_tokens": 100_000}, steps, [call]) is None
