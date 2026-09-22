"""Drift detection — cheap first, LLM last (§8.6.2).

Tier 0: deterministic watchdogs (no LLM). Tier 1: drift scoring from embeddings +
citation/constraint signals (no LLM). Tier 2 (LLM judge) lives elsewhere."""

from __future__ import annotations

import math
from collections import Counter

# Tier-1 drift-score weights (sum ~1.0): trajectory-vs-anchor divergence dominates,
# uncited claims and constraint violations add on top.
_W_DIVERGENCE = 0.6
_W_UNCITED = 0.25
_W_VIOLATIONS = 0.15
_VIOLATIONS_CAP = 5


# --- Tier 0: deterministic watchdogs (no LLM) ---


def loop_detected(tool_calls: list[str], *, window: int = 6, repeat: int = 3) -> bool:
    """True if any tool-call signature repeats >= `repeat` times in the last
    `window` calls (a stuck tool-loop pathology)."""
    if not tool_calls:
        return False
    counts = Counter(tool_calls[-window:])
    return any(c >= repeat for c in counts.values())


def token_burn_alert(tokens_since_progress: int, *, budget: int) -> bool:
    """True if the agent has burned past `budget` tokens without a task-state change."""
    return budget > 0 and tokens_since_progress >= budget


def tool_error_alert(errors: int, total: int, *, max_rate: float = 0.5) -> bool:
    """True if the tool-error rate over recent calls exceeds `max_rate`."""
    if total <= 0:
        return False
    return (errors / total) >= max_rate


# --- Tier 1: drift scoring (embedding math, no LLM) ---


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def compute_drift_score(
    *,
    anchor_embedding: list[float] | None,
    trajectory_embedding: list[float] | None,
    uncited_ratio: float = 0.0,
    constraint_violations: int = 0,
) -> float:
    """Weighted drift score in [0, 1]. 0 = on-anchor; 1 = fully drifted.

    Divergence = 1 - cosine(trajectory, anchor). When embeddings are absent,
    divergence contributes 0 (only citation/constraint signals count)."""
    if anchor_embedding and trajectory_embedding:
        divergence = 1.0 - cosine_similarity(anchor_embedding, trajectory_embedding)
    else:
        divergence = 0.0
    violations = min(1.0, constraint_violations / _VIOLATIONS_CAP)
    score = (
        _W_DIVERGENCE * divergence
        + _W_UNCITED * max(0.0, min(1.0, uncited_ratio))
        + _W_VIOLATIONS * violations
    )
    return max(0.0, min(1.0, score))


def verdict_from_score(score: float, thresholds: dict[str, float]) -> str:
    """Map a drift score to a checkpoint verdict using the policy thresholds
    (`steer` < `rewind`): below steer = on_track, up to rewind = drifting, else
    off_track."""
    steer = float(thresholds.get("steer", 0.35))
    rewind = float(thresholds.get("rewind", 0.55))
    if score < steer:
        return "on_track"
    if score < rewind:
        return "drifting"
    return "off_track"
