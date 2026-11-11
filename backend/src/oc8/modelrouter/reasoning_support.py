"""Which built-in models can think (spec 2026-11-11 §4).

One rule, read through the built-in `ProviderEntry.supports_reasoning`
declarations in `oc8.modelrouter.registry`, so the 0116 backfill, the seed
and the model form's prefill can never disagree. It is a guess the admin can
overrule with the model's "Can think" switch: a wrong "no" only hides the
control, and a wrong "yes" is caught by the router's one retry without the
level (`ModelRouter._retry_without_reasoning`).
"""

from __future__ import annotations

import re

_ANTHROPIC = re.compile(r"^claude-(3[.-]7-sonnet|(opus|sonnet|haiku)-[4-9])")
_OPENAI = re.compile(r"^(o[1-9]|gpt-5|gpt-oss|codex)")
_OPEN_WEIGHTS = re.compile(r"^(qwen3|qwq|deepseek-r1|gpt-oss|magistral|phi4-reasoning|cogito)")
#: Ollama models that take `think: "low"|"medium"|"high"` instead of a bool.
_THINK_LEVELS = re.compile(r"^gpt-oss")


def _base(model: str) -> str:
    """The model name without a vendor or registry prefix ("openai/o3",
    "hf.co/unsloth/Qwen3-8B-GGUF"), lowercased."""
    return model.strip().lower().rsplit("/", 1)[-1]


def anthropic_thinks(model: str) -> bool:
    return bool(_ANTHROPIC.match(_base(model)))


def openai_thinks(model: str) -> bool:
    return bool(_OPENAI.match(_base(model)))


def openai_compatible_thinks(model: str) -> bool:
    """A gateway (OpenRouter, LiteLLM, vLLM, …) can serve any family."""
    base = _base(model)
    return bool(_OPENAI.match(base) or _OPEN_WEIGHTS.match(base) or _ANTHROPIC.match(base))


def ollama_thinks(model: str) -> bool:
    return bool(_OPEN_WEIGHTS.match(_base(model)))


def ollama_has_think_levels(model: str) -> bool:
    return bool(_THINK_LEVELS.match(_base(model)))
