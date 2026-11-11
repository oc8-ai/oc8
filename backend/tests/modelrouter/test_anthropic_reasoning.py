"""The Anthropic request shape for a reasoning level (spec 2026-11-11 §6).

Verified against the Anthropic Python SDK 2.8.0 (`MessageCreateParams`:
`output_config: OutputConfigParam` with `effort`, `thinking:
ThinkingConfigParam`) and the docs pages build-with-claude/effort and
build-with-claude/thinking (2026-11-11): effort lives in
`output_config.effort` (GA, no beta header), never top-level; models from
Claude 4.6 on think with `{"type": "adaptive"}`, older ones with
`{"type": "enabled", "budget_tokens": N}` (>= 1024 and < max_tokens); only
those adaptive models and Claude Opus 4.5 take an effort at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from oc8.modelrouter.adapters.anthropic import _build_payload
from oc8.modelrouter.reasoning_support import (
    anthropic_has_effort,
    anthropic_thinking_style,
)
from oc8.modelrouter.types import (
    CompletionRequest,
    ModelParams,
    NeutralMessage,
    NeutralTool,
    ToolCall,
)


def _req(
    model: str,
    *,
    reasoning: Any = None,
    effort: str | None = None,
    extra: dict[str, Any] | None = None,
    max_tokens: int = 8192,
    messages: list[NeutralMessage] | None = None,
    tools: list[NeutralTool] | None = None,
) -> CompletionRequest:
    return CompletionRequest(
        provider="anthropic",
        model=model,
        messages=messages or [NeutralMessage(role="user", content="hi")],
        tools=tools or [],
        params=ModelParams(
            temperature=0.3,
            max_tokens=max_tokens,
            reasoning=reasoning,
            effort=effort,
            extra=extra,
        ),
    )


@pytest.mark.parametrize(
    ("model", "style", "effort"),
    [
        ("claude-opus-5-5", "adaptive", True),
        ("claude-sonnet-5", "adaptive", True),
        ("claude-haiku-5-5", "adaptive", True),
        ("claude-fable-5-1", "adaptive", True),
        ("claude-mythos-5-1", "adaptive", True),
        ("claude-opus-4-8", "adaptive", True),
        ("claude-opus-4-6", "adaptive", True),
        ("claude-sonnet-4-6", "adaptive", True),
        ("anthropic/claude-sonnet-4.6", "adaptive", True),
        ("claude-opus-4-5-20251101", "budget", True),
        ("claude-sonnet-4-5-20250929", "budget", False),
        ("claude-haiku-4-5", "budget", False),
        ("claude-opus-4-1-20250805", "budget", False),
        ("claude-opus-4-20250514", "budget", False),
        ("claude-3-7-sonnet-20250219", "budget", False),
        ("claude-3-5-sonnet-20241022", None, False),
        ("claude-3-haiku", None, False),
        # A name no rule knows reads as a current model: the router's one
        # retry without a level covers a wrong guess.
        ("claude-something-new", "adaptive", True),
    ],
)
def test_thinking_style_and_effort_per_model(model: str, style: str | None, effort: bool) -> None:
    assert anthropic_thinking_style(model) == style
    assert anthropic_has_effort(model) is effort


@pytest.mark.parametrize(
    ("level", "sent"), [("fast", "low"), ("balanced", "medium"), ("thorough", "high")]
)
def test_a_level_is_sent_as_output_config_effort(level: str, sent: str) -> None:
    payload = _build_payload(_req("claude-opus-5-5", reasoning=level))
    assert payload["output_config"] == {"effort": sent}
    assert "effort" not in payload


@pytest.mark.parametrize("level", ["balanced", "thorough"])
def test_balanced_and_thorough_turn_adaptive_thinking_on(level: str) -> None:
    payload = _build_payload(_req("claude-opus-4-8", reasoning=level))
    # Opus 4.7+ hide the thinking text unless asked; chat shows it.
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    # Sampling parameters are incompatible with thinking.
    assert "temperature" not in payload


def test_claude_4_6_gets_adaptive_thinking_without_a_display() -> None:
    """4.6 already returns summarized thinking; `display` arrived with 4.7."""
    payload = _build_payload(_req("claude-sonnet-4-6", reasoning="thorough"))
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}


def test_fast_sends_the_lowest_effort_and_forces_no_thinking() -> None:
    payload = _build_payload(_req("claude-opus-4-8", reasoning="fast"))
    assert payload["output_config"] == {"effort": "low"}
    assert "thinking" not in payload
    assert payload["temperature"] == 0.3


@pytest.mark.parametrize(("level", "budget"), [("balanced", 4096), ("thorough", 16384)])
def test_an_older_model_thinks_with_a_budget_and_gets_no_effort(level: str, budget: int) -> None:
    payload = _build_payload(_req("claude-haiku-4-5", reasoning=level, max_tokens=64000))
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": budget}
    assert "output_config" not in payload
    assert "effort" not in payload
    assert "temperature" not in payload


def test_a_budget_always_stays_below_max_tokens() -> None:
    payload = _build_payload(_req("claude-sonnet-4-5", reasoning="thorough", max_tokens=4096))
    budget = payload["thinking"]["budget_tokens"]
    assert 1024 <= budget < 4096


def test_no_budgeted_thinking_when_max_tokens_leaves_no_room() -> None:
    payload = _build_payload(_req("claude-haiku-4-5", reasoning="thorough", max_tokens=1024))
    assert "thinking" not in payload
    assert payload["temperature"] == 0.3


def test_opus_4_5_gets_both_an_effort_and_a_budget() -> None:
    payload = _build_payload(_req("claude-opus-4-5", reasoning="thorough", max_tokens=64000))
    assert payload["output_config"] == {"effort": "high"}
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16384}


def test_fast_on_an_older_model_sends_nothing() -> None:
    payload = _build_payload(_req("claude-haiku-4-5", reasoning="fast"))
    assert "thinking" not in payload
    assert "output_config" not in payload


def test_a_model_that_cannot_think_gets_neither_field() -> None:
    payload = _build_payload(_req("claude-3-5-sonnet-20241022", reasoning="thorough"))
    assert "thinking" not in payload
    assert "output_config" not in payload


def test_a_legacy_free_form_effort_goes_into_output_config() -> None:
    payload = _build_payload(_req("claude-sonnet-5", effort="max"))
    assert payload["output_config"] == {"effort": "max"}
    assert "effort" not in payload
    assert "thinking" not in payload


def test_a_legacy_effort_is_dropped_for_a_model_without_effort() -> None:
    payload = _build_payload(_req("claude-haiku-4-5", effort="high"))
    assert "output_config" not in payload
    assert "effort" not in payload


def test_the_level_wins_over_a_free_form_effort_and_only_one_shape_is_sent() -> None:
    payload = _build_payload(_req("claude-sonnet-5", reasoning="fast", effort="max"))
    assert payload["output_config"] == {"effort": "low"}
    assert "effort" not in payload


def test_extra_cannot_override_the_thinking_or_effort_the_adapter_set() -> None:
    payload = _build_payload(
        _req(
            "claude-opus-4-8",
            reasoning="thorough",
            extra={
                "thinking": {"type": "disabled"},
                "output_config": {"effort": "low", "format": {"type": "json_schema"}},
                "effort": "low",
            },
        )
    )
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    # A non-conflicting output_config key still reaches the provider.
    assert payload["output_config"] == {"effort": "high", "format": {"type": "json_schema"}}
    assert "effort" not in payload


def test_extra_thinking_and_output_config_pass_through_without_a_level() -> None:
    """With no level and no configured effort the operator's own keys apply."""
    payload = _build_payload(
        _req(
            "claude-opus-4-8",
            extra={"thinking": {"type": "adaptive"}, "output_config": {"effort": "xhigh"}},
        )
    )
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "xhigh"}


def test_thinking_with_tools_replays_the_signed_block_first() -> None:
    signed = {"type": "thinking", "thinking": "look it up", "signature": "sig"}
    messages = [
        NeutralMessage(role="user", content="weather?"),
        NeutralMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="t1", name="weather", arguments={})],
            replay_blocks=[signed],
        ),
        NeutralMessage(role="tool", content="sunny", tool_call_id="t1"),
    ]
    tools = [NeutralTool(name="weather", description="w", parameters={"type": "object"})]
    payload = _build_payload(
        _req("claude-sonnet-4-6", reasoning="balanced", messages=messages, tools=tools)
    )
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["tools"][0]["name"] == "weather"
    assistant = payload["messages"][1]
    assert assistant["content"][0] == signed
    assert assistant["content"][1]["type"] == "tool_use"
    # No forced tool choice: thinking rejects it.
    assert "tool_choice" not in payload
