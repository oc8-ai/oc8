"""Each built-in adapter maps the level to its own API (spec 2026-11-11 §6),
and keeps today's behaviour when only a free-form effort is set."""

from __future__ import annotations

from typing import Any

import pytest

from oc8.modelrouter.adapters._openai_common import build_payload
from oc8.modelrouter.adapters.anthropic import _build_payload as anthropic_payload
from oc8.modelrouter.adapters.chatgpt_subscription import build_responses_payload
from oc8.modelrouter.adapters.ollama import _build_payload as ollama_payload
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage


def _req(
    model: str,
    *,
    reasoning: Any = None,
    effort: str | None = None,
    extra: dict[str, Any] | None = None,
) -> CompletionRequest:
    return CompletionRequest(
        provider="x",
        model=model,
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(reasoning=reasoning, effort=effort, extra=extra),
    )


@pytest.mark.parametrize(
    ("level", "sent"), [("fast", "low"), ("balanced", "medium"), ("thorough", "high")]
)
def test_anthropic_sends_effort(level: str, sent: str) -> None:
    assert anthropic_payload(_req("claude-sonnet-5", reasoning=level))["effort"] == sent


def test_anthropic_level_wins_over_a_free_form_effort() -> None:
    payload = anthropic_payload(_req("claude-sonnet-5", reasoning="fast", effort="max"))
    assert payload["effort"] == "low"


def test_anthropic_keeps_a_free_form_effort_without_a_level() -> None:
    assert anthropic_payload(_req("claude-sonnet-5", effort="max"))["effort"] == "max"


@pytest.mark.parametrize(
    ("level", "sent"), [("fast", "low"), ("balanced", "medium"), ("thorough", "high")]
)
def test_openai_compatible_sends_reasoning_effort(level: str, sent: str) -> None:
    assert build_payload(_req("o3", reasoning=level))["reasoning_effort"] == sent


def test_openai_compatible_sends_nothing_without_a_level() -> None:
    assert "reasoning_effort" not in build_payload(_req("o3", effort="high"))


def test_an_extra_param_cannot_clobber_reasoning_effort() -> None:
    payload = build_payload(_req("o3", reasoning="fast", extra={"reasoning_effort": "high"}))
    assert payload["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    ("level", "sent"), [("fast", "low"), ("balanced", "medium"), ("thorough", "high")]
)
def test_chatgpt_subscription_sends_reasoning_effort(level: str, sent: str) -> None:
    assert build_responses_payload(_req("gpt-5", reasoning=level))["reasoning"] == {"effort": sent}


def test_chatgpt_subscription_sends_nothing_without_a_level() -> None:
    assert "reasoning" not in build_responses_payload(_req("gpt-5", effort="high"))


@pytest.mark.parametrize(
    ("level", "think"), [("fast", False), ("balanced", True), ("thorough", True)]
)
def test_ollama_sends_think(level: str, think: bool) -> None:
    assert ollama_payload(_req("qwen3:8b", reasoning=level), stream=False)["think"] is think


@pytest.mark.parametrize(
    ("level", "think"), [("fast", "low"), ("balanced", "medium"), ("thorough", "high")]
)
def test_ollama_think_levels_for_models_that_have_them(level: str, think: str) -> None:
    assert ollama_payload(_req("gpt-oss:20b", reasoning=level), stream=True)["think"] == think


def test_ollama_sends_no_think_without_a_level() -> None:
    assert "think" not in ollama_payload(_req("qwen3:8b", effort="high"), stream=False)
