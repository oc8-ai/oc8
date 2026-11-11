"""Where a run's sampling parameters come from.

Both runtimes used to hardcode ``ModelParams(temperature=0.0, max_tokens=512)``,
and ``ModelConfig.params`` was read only for ``base_url``. Tuning an agent
therefore required a code change -- and temperature 0.0 means one task yields
byte-identical output every time, which reads as "the agent has no ideas" when it
is really "the agent was told to be deterministic".

Resolution order, narrowest first:

    agent.definition["model_params"]  ->  ModelConfig.params  ->  framework default

Same shape as ``max_steps`` (§8.3), which an agent plugin may already raise per
agent. Shared by both runtimes on purpose: sampling drifting apart between
in-process and isolated runs would be invisible and maddening to debug.

The reasoning level follows the same order with the chat's pick in front
(spec 2026-11-11 §5): chat session -> agent effort -> model config effort ->
balanced, and nothing at all for a model whose `supports_reasoning` is off.

Values come from free-form JSONB an operator edits, so nothing here may raise: a
typo degrades to the default and a wild number is clamped, because a failed run
is a much worse answer to a misconfiguration than a sane one.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from oc8 import models as m
from oc8.config import get_settings
from oc8.modelrouter.levels import CHAT_LEVEL_KEY, as_level, resolve_reasoning
from oc8.modelrouter.types import ModelParams, ReasoningLevel

# Slightly above zero: enough variation that an agent doesn't repeat itself word
# for word, low enough to stay reliable at tool calling.
DEFAULT_TEMPERATURE = 0.3
# Fallback answer budget when the settings cannot be read. The real default is
# `Settings.default_max_tokens` (OC8_DEFAULT_MAX_TOKENS): room for a multi-line
# tool call plus the reasoning a thinking model does before it. 1536 used to be
# the default and was spent entirely on reasoning by such models.
DEFAULT_MAX_TOKENS = 4096

# Providers hard-error outside this range, which would surface as a failed run
# rather than as the configuration mistake it is.
_MIN_TEMPERATURE = 0.0
_MAX_TEMPERATURE = 2.0

# A reasoning-capable model can spend its whole completion budget on hidden
# reasoning tokens and hit max_tokens before writing anything visible --
# `stop_reason == "length"` with no tool call and no usable text is that, not
# a real stop (see engine.py/internal_agent.py's use of this). One retry with
# a doubled budget is cheap insurance against a truncation that would
# otherwise silently read as a completed task with nothing done.
LENGTH_RETRY_MAX_TOKENS_MULTIPLIER = 2


def default_max_tokens() -> int:
    """`OC8_DEFAULT_MAX_TOKENS`, or `DEFAULT_MAX_TOKENS` if it is unusable."""
    try:
        configured = get_settings().default_max_tokens
    except Exception:  # settings that fail to load must not fail a run
        return DEFAULT_MAX_TOKENS
    return _max_tokens(configured) or DEFAULT_MAX_TOKENS


def provider_max_tokens(provider: str | None) -> int | None:
    """The most max_tokens `provider` accepts (`OC8_PROVIDER_MAX_TOKENS`), or None."""
    if not provider:
        return None
    try:
        return get_settings().provider_max_tokens_map.get(provider)
    except Exception:
        return None


def cap_for_provider(max_tokens: int, provider: str | None) -> int:
    """`max_tokens` cut down to the provider's cap, if it has one."""
    cap = provider_max_tokens(provider)
    return min(max_tokens, cap) if cap is not None else max_tokens


def bumped_for_length_retry(params: ModelParams, *, provider: str | None = None) -> ModelParams:
    """`params` with twice the budget, never above `provider`'s cap.

    The result may carry the same budget as `params` when that is already at
    the cap; a caller should then skip the retry, which could only fail the
    same way again.
    """
    # `replace`, not a field-by-field copy: a new field (the reasoning level
    # was one) must never be dropped by the retry that doubles the budget.
    return replace(
        params,
        max_tokens=cap_for_provider(
            params.max_tokens * LENGTH_RETRY_MAX_TOKENS_MULTIPLIER, provider
        ),
    )


def budget_exhausted_message(max_tokens: int) -> str:
    """Why a step failed with nothing to show, and what the operator can do.

    Stored as the run's output and shown as its failure reason, so it names the
    budget the last attempt had and the three settings that move it.
    """
    return (
        f"The model used its whole answer budget ({max_tokens:,} tokens) without "
        "producing an answer or tool call, even after one retry. Reasoning models "
        "spend part of this budget thinking. To fix it, raise max_tokens for this "
        "agent or its model connection (or OC8_DEFAULT_MAX_TOKENS for all), or "
        "lower the model's reasoning effort."
    )


def _temperature(raw: Any) -> float | None:
    """A usable temperature, or None if this value says nothing."""
    # bool is an int subclass; True would silently become 1.0.
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return max(_MIN_TEMPERATURE, min(_MAX_TEMPERATURE, float(raw)))


def _max_tokens(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw > 0 else None


def _effort(raw: Any) -> str | None:
    """A usable effort value, or None if this value says nothing.

    Deliberately not constrained to a fixed set ("low"/"medium"/"high", ...):
    the provider defines what it accepts, and that set changes on the
    provider's own schedule -- validating against today's guess would only
    reject tomorrow's valid value. Any non-empty string is forwarded as-is.
    """
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    return trimmed or None


def _extra(raw: Any) -> dict[str, Any] | None:
    """A usable raw-parameter dict, or None if this value says nothing.

    Keys are whatever the operator typed; oc8 has no fixed schema for them
    (the provider does) and does not validate values beyond "is this a dict".
    """
    if not isinstance(raw, dict) or not raw:
        return None
    return raw


def configured_effort(
    config: m.ModelConfig | None, definition: dict[str, Any] | None
) -> str | None:
    """The free-form effort an operator configured, narrowest first: the
    agent's `model_params.effort`, then the model config's `params.effort`.
    None when neither says anything usable. Shared with the chat model picker,
    so its "Default · ..." names what a run would use."""
    model_params = (definition or {}).get("model_params")
    for source in (
        model_params if isinstance(model_params, dict) else {},
        (config.params or {}) if config is not None else {},
    ):
        if isinstance(source, dict) and (e := _effort(source.get("effort"))) is not None:
            return e
    return None


def _chat_level(definition: dict[str, Any] | None) -> ReasoningLevel | None:
    model_params = (definition or {}).get("model_params")
    return as_level(model_params.get(CHAT_LEVEL_KEY)) if isinstance(model_params, dict) else None


def resolve_params(
    config: m.ModelConfig | None,
    *,
    agent: m.Agent | None = None,
    definition: dict[str, Any] | None = None,
) -> ModelParams:
    """Sampling parameters for one model call.

    ``agent`` may be None for callers that have no agent (e.g. the coding loop).
    ``definition`` is the run's PINNED agent definition (`resolve_version`);
    when given it is read instead of ``agent.definition``, so a mid-run edit
    of the live row never changes the sampling a running run uses.
    """
    if definition is None and agent is not None:
        definition = agent.definition
    temperature = DEFAULT_TEMPERATURE
    max_tokens = default_max_tokens()
    extra: dict[str, Any] = {}

    # Widest scope first, so a narrower one simply overwrites it. Each key is
    # considered independently: setting only temperature on an agent must not
    # discard the model config's max_tokens. `extra` follows the same rule one
    # level deeper -- an agent overriding one raw key must not drop the
    # model's own raw keys it didn't mention.
    for source in (
        (config.params or {}) if config is not None else {},
        (definition or {}).get("model_params") or {},
    ):
        if not isinstance(source, dict):
            continue
        if (t := _temperature(source.get("temperature"))) is not None:
            temperature = t
        if (mt := _max_tokens(source.get("max_tokens"))) is not None:
            max_tokens = mt
        if (ex := _extra(source.get("extra"))) is not None:
            extra.update(ex)

    reasoning, effort = resolve_reasoning(
        supports=None if config is None else bool(config.supports_reasoning),
        configured=configured_effort(config, definition),
        chat_level=_chat_level(definition),
    )
    return ModelParams(
        temperature=temperature,
        # A provider that accepts fewer tokens gets its cap instead of an
        # error that would fail the run.
        max_tokens=cap_for_provider(max_tokens, config.provider if config is not None else None),
        effort=effort,
        reasoning=reasoning,
        extra=extra or None,
    )
