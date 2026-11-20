"""OpenAI caches a prompt's repeated prefix on its own and reports the cached
part in `prompt_tokens_details.cached_tokens` (Chat Completions) or
`input_tokens_details.cached_tokens` (Responses). It is counted inside the
prompt tokens, so `tokens_in` stays the whole prompt and only the split is
added. OpenAI bills no separate cache write."""

from __future__ import annotations

import pytest

from oc8.metering.pricing import cache_read_rate, cost_micros_from_price
from oc8.modelrouter.adapters._openai_common import parse_response
from oc8.modelrouter.adapters.chatgpt_subscription import parse_responses_result
from oc8.modelrouter.streaming import _chunk_from_openai


def _chat_response(usage: dict[str, object]) -> dict[str, object]:
    return {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": usage,
    }


def test_a_completion_reports_its_cached_prompt_tokens() -> None:
    usage = parse_response(
        _chat_response(
            {
                "prompt_tokens": 2006,
                "completion_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 1920},
            }
        ),
        provider="openai",
        model="gpt-5",
    ).usage
    assert (usage.tokens_in, usage.cache_read_tokens, usage.cache_write_tokens) == (2006, 1920, 0)


def test_a_completion_without_details_has_no_cached_tokens() -> None:
    usage = parse_response(
        _chat_response({"prompt_tokens": 10, "completion_tokens": 2}),
        provider="openai_compatible",
        model="llama3",
    ).usage
    assert usage.cache_read_tokens == 0


def test_a_streamed_usage_chunk_reports_its_cached_prompt_tokens() -> None:
    chunk = _chunk_from_openai(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 1500,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 1024},
            },
        }
    )
    assert chunk is not None and chunk.usage is not None
    assert (chunk.usage.tokens_in, chunk.usage.cache_read_tokens) == (1500, 1024)


def test_a_responses_result_reports_its_cached_input_tokens() -> None:
    usage = parse_responses_result(
        {
            "output": [],
            "usage": {
                "input_tokens": 3000,
                "output_tokens": 40,
                "input_tokens_details": {"cached_tokens": 2048},
            },
        },
        provider="openai_chatgpt",
        model="gpt-5-codex",
    ).usage
    assert (usage.tokens_in, usage.cache_read_tokens) == (3000, 2048)


def test_a_malformed_cached_count_is_ignored() -> None:
    usage = parse_response(
        _chat_response(
            {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": "lots"},
            }
        ),
        provider="openai",
        model="gpt-4o",
    ).usage
    assert usage.cache_read_tokens == 0


@pytest.mark.parametrize(
    ("model", "rate"),
    [
        ("claude-sonnet-4-5", 0.1),
        ("gpt-5", 0.1),
        ("gpt-5-mini", 0.1),
        ("gpt-4.1", 0.25),
        ("gpt-4.1-mini", 0.25),
        ("o3", 0.25),
        ("o4-mini", 0.25),
        ("gpt-4o", 0.5),
        ("gpt-4o-mini", 0.5),
        ("o1", 0.5),
        ("openai/gpt-4o-2024-08-06", 0.5),
    ],
)
def test_the_cache_read_rate_follows_the_model_family(model: str, rate: float) -> None:
    assert cache_read_rate(model) == rate


def test_a_gpt_4o_cache_read_costs_half_the_input_price() -> None:
    # 1000 prompt tokens, 800 of them cached, at $2.50 in / $10 out per 1M.
    cost = cost_micros_from_price((2.5, 10.0), 1000, 0, cache_read_tokens=800, model="gpt-4o")
    assert cost == round(200 * 2.5 + 800 * 2.5 * 0.5)


def test_without_a_model_the_cache_read_keeps_the_claude_rate() -> None:
    cost = cost_micros_from_price((3.0, 15.0), 1000, 0, cache_read_tokens=1000)
    assert cost == round(1000 * 3.0 * 0.1)
