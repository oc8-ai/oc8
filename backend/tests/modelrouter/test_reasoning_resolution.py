"""Where a run's reasoning level comes from (spec 2026-11-11 §5)."""

from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.modelrouter.sampling import bumped_for_length_retry, configured_effort, resolve_params
from oc8.modelrouter.types import ModelParams


def _config(params: dict[str, Any] | None = None, *, thinks: bool = True) -> m.ModelConfig:
    return m.ModelConfig(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        provider="anthropic",
        model="claude-sonnet-5",
        params=params or {},
        supports_reasoning=thinks,
    )


def _definition(effort: str | None = None, chat: str | None = None) -> dict[str, Any]:
    model_params: dict[str, Any] = {}
    if effort is not None:
        model_params["effort"] = effort
    if chat is not None:
        model_params["reasoning"] = chat
    return {"model_params": model_params}


def test_nothing_configured_is_balanced() -> None:
    p = resolve_params(_config(), definition={})
    assert (p.reasoning, p.effort) == ("balanced", None)


def test_the_model_configs_legacy_value_maps() -> None:
    p = resolve_params(_config({"effort": "HIGH"}), definition={})
    assert (p.reasoning, p.effort) == ("thorough", "HIGH")


def test_the_agent_beats_the_model_config() -> None:
    p = resolve_params(_config({"effort": "high"}), definition=_definition(effort="low"))
    assert (p.reasoning, p.effort) == ("fast", "low")


def test_a_custom_value_is_kept_as_effort_only() -> None:
    p = resolve_params(_config({"effort": "max"}), definition={})
    assert (p.reasoning, p.effort) == (None, "max")


def test_the_chats_pick_beats_the_agent_and_the_model() -> None:
    p = resolve_params(
        _config({"effort": "high"}), definition=_definition(effort="medium", chat="fast")
    )
    assert (p.reasoning, p.effort) == ("fast", None)


def test_an_invalid_chat_pick_is_ignored() -> None:
    p = resolve_params(_config(), definition=_definition(chat="max"))
    assert p.reasoning == "balanced"


def test_a_chat_pick_on_a_model_that_cannot_think_sends_nothing() -> None:
    p = resolve_params(
        _config({"effort": "high"}, thinks=False), definition=_definition(effort="low", chat="fast")
    )
    assert (p.reasoning, p.effort) == (None, None)


def test_without_a_model_config_the_legacy_effort_still_flows() -> None:
    p = resolve_params(None, definition=_definition(effort="high", chat="fast"))
    assert (p.reasoning, p.effort) == (None, "high")


def test_configured_effort_reads_narrowest_first() -> None:
    assert configured_effort(_config({"effort": "high"}), _definition(effort="low")) == "low"
    assert configured_effort(_config({"effort": "high"}), {}) == "high"
    assert configured_effort(_config({"effort": "  "}), None) is None
    assert configured_effort(None, None) is None


def test_a_length_retry_keeps_the_level() -> None:
    bumped = bumped_for_length_retry(
        ModelParams(temperature=0.5, max_tokens=100, effort="high", reasoning="thorough")
    )
    assert bumped.max_tokens == 200
    assert (bumped.reasoning, bumped.effort) == ("thorough", "high")
