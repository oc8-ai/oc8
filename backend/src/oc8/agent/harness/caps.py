"""Per-model capability flags (spec §3.4).

Resolved from `model_config.params` -- the same dict that already carries
`supports_vision` -- so a provider capa declares them in its model manifest.
Unknown or malformed values fall back to the conservative default: a typo in
a manifest must never take a run down, and the harness must never branch on
a model *name*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ModelCaps:
    parallel_tool_calls: bool = False
    #: False for providers that reject a system message after the first turn.
    mid_conversation_system: bool = True
    #: False -> deferred tool retrieval pins every tool at step 1.
    tool_list_may_change: bool = True
    code_mode: bool = False
    context_window_tokens: int = 128_000
    prompt_caching: bool = False
    #: None -> the run's own model summarises on compaction.
    compaction_summary_model: str | None = None


def resolve_caps(params: Mapping[str, Any] | None) -> ModelCaps:
    raw = params or {}
    caps = ModelCaps()

    # Boolean flags
    if isinstance(raw.get("parallel_tool_calls"), bool):
        caps = replace(caps, parallel_tool_calls=raw["parallel_tool_calls"])
    if isinstance(raw.get("mid_conversation_system"), bool):
        caps = replace(caps, mid_conversation_system=raw["mid_conversation_system"])
    if isinstance(raw.get("tool_list_may_change"), bool):
        caps = replace(caps, tool_list_may_change=raw["tool_list_may_change"])
    if isinstance(raw.get("code_mode"), bool):
        caps = replace(caps, code_mode=raw["code_mode"])
    if isinstance(raw.get("prompt_caching"), bool):
        caps = replace(caps, prompt_caching=raw["prompt_caching"])

    # Context window
    window = raw.get("context_window_tokens")
    # bool is an int subclass; `True` must not read as a 1-token window.
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        caps = replace(caps, context_window_tokens=window)

    # Summary model
    summary_model = raw.get("compaction_summary_model")
    if isinstance(summary_model, str) and summary_model.strip():
        caps = replace(caps, compaction_summary_model=summary_model)

    return caps
