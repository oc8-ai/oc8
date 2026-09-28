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

Values come from free-form JSONB an operator edits, so nothing here may raise: a
typo degrades to the default and a wild number is clamped, because a failed run
is a much worse answer to a misconfiguration than a sane one.
"""

from __future__ import annotations

from typing import Any

from oc8 import models as m
from oc8.modelrouter.types import ModelParams

# Slightly above zero: enough variation that an agent doesn't repeat itself word
# for word, low enough to stay reliable at tool calling.
DEFAULT_TEMPERATURE = 0.3
# Room for a multi-line tool call (several order lines plus a note) and a short
# summary. The old 512 truncated exactly that kind of call.
DEFAULT_MAX_TOKENS = 1536

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


def bumped_for_length_retry(params: ModelParams) -> ModelParams:
    return ModelParams(
        temperature=params.temperature,
        max_tokens=params.max_tokens * LENGTH_RETRY_MAX_TOKENS_MULTIPLIER,
        effort=params.effort,
        extra=params.extra,
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
    max_tokens = DEFAULT_MAX_TOKENS
    effort: str | None = None
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
        if (e := _effort(source.get("effort"))) is not None:
            effort = e
        if (ex := _extra(source.get("extra"))) is not None:
            extra.update(ex)

    return ModelParams(
        temperature=temperature,
        max_tokens=max_tokens,
        effort=effort,
        extra=extra or None,
    )
