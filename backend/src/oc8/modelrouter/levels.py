"""The three reasoning levels a chat member picks from (spec 2026-11-11 §2, §5).

`ReasoningLevel` itself lives in `oc8.modelrouter.types`, so `ModelParams` can
name it without importing this module. This module owns how a configured
free-form `effort` reads as a level, and the one resolution rule shared by
`resolve_params` and the chat model picker.
"""

from __future__ import annotations

from typing import Final

from oc8.modelrouter.types import ReasoningLevel

LEVELS: Final[tuple[ReasoningLevel, ...]] = ("fast", "balanced", "thorough")
DEFAULT_LEVEL: Final[ReasoningLevel] = "balanced"
#: What the picker's "Default" entry names for a configured value that maps to
#: no level ("Default · Custom setting").
CUSTOM: Final = "custom"
#: The pinned definition's `model_params` key that carries a chat's pick into
#: `resolve_params` (written by `oc8.chat.model_choice.apply_effort_override`,
#: never stored on an agent).
CHAT_LEVEL_KEY: Final = "reasoning"

_LEGACY: Final[dict[str, ReasoningLevel]] = {
    "low": "fast",
    "medium": "balanced",
    "high": "thorough",
}


def as_level(raw: object) -> ReasoningLevel | None:
    """`raw` if it is exactly one of the three levels, else None. For values a
    member picked (a chat session, a request body), so nothing is folded."""
    if raw == "fast":
        return "fast"
    if raw == "balanced":
        return "balanced"
    if raw == "thorough":
        return "thorough"
    return None


def level_for(configured: str | None) -> ReasoningLevel | None:
    """The level a configured free-form effort reads as: low/medium/high in any
    case, or a level key; None for any other value."""
    if configured is None:
        return None
    key = configured.strip().lower()
    return _LEGACY.get(key) or as_level(key)


def default_display(configured: str | None) -> str:
    """What "Default · …" names: the level `configured` reads as, "custom"
    for a value that maps to none, and balanced when nothing is configured."""
    if configured is None:
        return DEFAULT_LEVEL
    return level_for(configured) or CUSTOM


def resolve_reasoning(
    *,
    supports: bool | None,
    configured: str | None,
    chat_level: ReasoningLevel | None,
) -> tuple[ReasoningLevel | None, str | None]:
    """`(ModelParams.reasoning, ModelParams.effort)` for one model call.

    `supports` is the model config's `supports_reasoning`, or None when the run
    has no model config at all. A run without a config keeps today's legacy
    behaviour: the free-form effort goes through and no level is sent, since
    nothing says the model can think. A model that cannot think gets neither
    field (spec §5). Otherwise the chat's pick wins, then a configured value
    (kept verbatim as `effort` beside the level it reads as), then balanced.
    """
    if supports is None:
        return None, configured
    if not supports:
        return None, None
    if chat_level is not None:
        return chat_level, None
    if configured is None:
        return DEFAULT_LEVEL, None
    return level_for(configured), configured
