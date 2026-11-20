"""Which built-in models can think (spec 2026-11-11 §4).

One rule, read through the built-in `ProviderEntry.supports_reasoning`
declarations in `oc8.modelrouter.registry`, so the 0116 backfill, the seed
and the model form's prefill can never disagree. It is a guess the admin can
overrule with the model's "Can think" switch: a wrong "no" only hides the
control, and a wrong "yes" is caught by the router's one retry without a
level (Task 5).
"""

from __future__ import annotations

import re
from typing import Literal

_ANTHROPIC = re.compile(r"^claude-(3[.-]7-sonnet|(opus|sonnet|haiku)-[4-9]|fable|mythos)")
_OPENAI = re.compile(r"^(o[1-9]\d?(?:-|$|:|\.)|gpt-5(?![\w.]*-chat)|gpt-oss|codex)")
_OPEN_WEIGHTS = re.compile(r"^(qwen3|qwq|deepseek-r1|gpt-oss|magistral|phi4-reasoning|cogito)")
#: Ollama models that take `think: "low"|"medium"|"high"` instead of a bool.
_THINK_LEVELS = re.compile(r"^gpt-oss")


def _base(model: str) -> str:
    """The model name without a vendor or registry prefix ("openai/o3",
    "hf.co/unsloth/Qwen3-8B-GGUF"), lowercased."""
    return model.strip().lower().rsplit("/", 1)[-1]


def anthropic_thinks(model: str) -> bool:
    return bool(_ANTHROPIC.match(_base(model)))


#: How a Claude model is told to think (Anthropic docs, build-with-claude/
#: thinking, "Configuring thinking"): `{"type": "adaptive"}` from Claude 4.6
#: on, `{"type": "enabled", "budget_tokens": N}` before that (3.7 to 4.5).
AnthropicThinkingStyle = Literal["adaptive", "budget"]

#: claude-opus-4-5, claude-sonnet-4.6, claude-opus-4-20250514 (no minor).
_CLAUDE_NEW = re.compile(r"^claude-(opus|sonnet|haiku)-(\d+)(?:[-.](\d{1,2})(?!\d))?")
#: claude-3-7-sonnet, claude-3-5-haiku, claude-3-opus.
_CLAUDE_OLD = re.compile(r"^claude-(\d+)(?:[-.](\d)(?!\d))?-(opus|sonnet|haiku)")


def _claude_version(model: str) -> tuple[str, int, int] | None:
    """(family, major, minor) of a Claude model name, or None for a name
    that does not spell a version (claude-fable-5-1, an alias)."""
    base = _base(model)
    if match := _CLAUDE_NEW.match(base):
        return match[1], int(match[2]), int(match[3] or 0)
    if match := _CLAUDE_OLD.match(base):
        return match[3], int(match[1]), int(match[2] or 0)
    return None


def anthropic_thinking_style(model: str) -> AnthropicThinkingStyle | None:
    """How `model` is asked to think, or None if it cannot.

    A name without a readable version (Fable, Mythos, an unknown alias) is
    taken for a current model: the router's one retry without a level covers
    a wrong guess, while a wrong "budget" would 400 on every current model.
    """
    version = _claude_version(model)
    if version is None:
        return "adaptive"
    _, major, minor = version
    if (major, minor) >= (4, 6):
        return "adaptive"
    if (major, minor) >= (3, 7):
        return "budget"
    return None


def anthropic_has_effort(model: str) -> bool:
    """Whether `model` takes `output_config.effort`: every adaptive-thinking
    model, plus Claude Opus 4.5 (docs, build-with-claude/effort)."""
    if anthropic_thinking_style(model) == "adaptive":
        return True
    return _claude_version(model) == ("opus", 4, 5)


def anthropic_hides_thinking(model: str) -> bool:
    """Whether `model` returns empty thinking text unless asked for a summary
    (`display: "omitted"` is the default from Claude Opus 4.7 on; 4.6 still
    summarizes by default and predates the `display` field)."""
    version = _claude_version(model)
    if version is None:
        return True
    _, major, minor = version
    return (major, minor) >= (4, 7)


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
