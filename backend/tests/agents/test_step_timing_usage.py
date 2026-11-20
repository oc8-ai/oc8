"""`note_usage`/`note_reasoning`: what a step used against its answer budget."""

from __future__ import annotations

from oc8.agent.harness.step_timing import finish_step, note_reasoning, note_usage, start_step
from oc8.modelrouter.reasoning import REASONING_STORE_LIMIT
from oc8.modelrouter.types import Usage


def test_usage_adds_up_across_a_retry_and_keeps_the_last_budget() -> None:
    rec = start_step(1)
    note_usage(rec, Usage(tokens_in=100, tokens_out=1536, reasoning_tokens=1536), max_tokens=1536)
    note_usage(rec, Usage(tokens_in=100, tokens_out=900, reasoning_tokens=None), max_tokens=3072)
    done = finish_step(rec)
    assert done["tokens_in"] == 200
    assert done["tokens_out"] == 2436
    assert done["reasoning_tokens"] == 1536
    assert done["max_tokens"] == 3072


def test_unreported_reasoning_tokens_stay_absent() -> None:
    rec = start_step(1)
    note_usage(rec, Usage(tokens_in=1, tokens_out=2), max_tokens=10)
    assert "reasoning_tokens" not in rec


def test_reasoning_is_stored_bounded_and_empty_reasoning_not_at_all() -> None:
    rec = start_step(1)
    note_reasoning(rec, None)
    assert "reasoning" not in rec
    note_reasoning(rec, "y" * (REASONING_STORE_LIMIT * 2))
    assert len(rec["reasoning"]) == REASONING_STORE_LIMIT
