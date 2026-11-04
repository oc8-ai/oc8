"""Step timing records and nearest-rank percentiles. No I/O.

A record also carries what the step's model calls used against their answer
budget (`note_usage`) and the reasoning the model reported (`note_reasoning`),
so a run view can show both next to the step's timing.
"""

from __future__ import annotations

import math
import time
from typing import Any

from oc8.modelrouter.reasoning import bound_reasoning


def percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _fmt(values: list[int], p: float) -> str:
    if p >= 95 and len(values) < 8:
        return "n/a (n<8)"
    got = percentile(values, p)
    return "n/a" if got is None else str(got)


def _bucket(records: list[dict], *, first: bool) -> list[int]:
    out = []
    for r in records:
        if r.get("ttft_ms") is None:
            continue
        is_first = int(r["step"]) == 1
        if is_first == first:
            out.append(int(r["ttft_ms"]))
    return out


def latency_lines(records: list[dict]) -> list[str]:
    """Markdown lines for one runtime's pooled steps. Empty records → ['No step timings.']"""
    if not records:
        return ["No step timings."]
    walls = [int(r["step_wall_ms"]) for r in records]
    models = [int(r["model_wait_ms"]) for r in records]
    tools = [int(r["tool_wait_ms"]) for r in records]
    ttfts = [int(r["ttft_ms"]) for r in records if r.get("ttft_ms") is not None]
    step1_ttfts = _bucket(records, first=True)
    step_gt1_ttfts = _bucket(records, first=False)
    return [
        f"steps: {len(records)}",
        f"p50 step_wall_ms: {_fmt(walls, 50)}",
        f"p95 step_wall_ms: {_fmt(walls, 95)}",
        f"p50 model_wait_ms: {_fmt(models, 50)}",
        f"p50 tool_wait_ms: {_fmt(tools, 50)}",
        f"p50 ttft_ms: {_fmt(ttfts, 50)}",
        f"ttft samples: {len(ttfts)}",
        f"p50 ttft_ms step 1: {'n/a' if not step1_ttfts else str(percentile(step1_ttfts, 50))}",
        f"p50 ttft_ms step>1: {'n/a' if not step_gt1_ttfts else str(percentile(step_gt1_ttfts, 50))}",
    ]


def start_step(step: int) -> dict:
    """Open a per-step timing record. `_t0` is internal; dropped by finish_step."""
    return {
        "step": step,
        "model_wait_ms": 0,
        "ttft_ms": None,
        "tool_wait_ms": 0,
        "step_wall_ms": 0,
        "_t0": time.monotonic(),
    }


def note_model(record: dict, *, model_wait_ms: int, ttft_ms: int | None) -> None:
    record["model_wait_ms"] = model_wait_ms
    record["ttft_ms"] = ttft_ms


def note_usage(record: dict[str, Any], usage: Any, *, max_tokens: int) -> None:
    """Add one model call's usage to the step; `max_tokens` is that call's budget.

    A step can make more than one call (the length retry, an escalation), so
    tokens add up, and the budget is the last call's -- the one whose answer
    the step went on with.
    """
    record["tokens_in"] = int(record.get("tokens_in") or 0) + int(usage.tokens_in or 0)
    record["tokens_out"] = int(record.get("tokens_out") or 0) + int(usage.tokens_out or 0)
    reasoning_tokens = getattr(usage, "reasoning_tokens", None)
    if reasoning_tokens is not None:
        record["reasoning_tokens"] = int(record.get("reasoning_tokens") or 0) + int(
            reasoning_tokens
        )
    record["max_tokens"] = int(max_tokens)


def note_reasoning(record: dict[str, Any], text: str | None) -> None:
    """Keep the reasoning behind the step's answer, bounded for storage."""
    bounded = bound_reasoning(text)
    if bounded:
        record["reasoning"] = bounded


def note_tools(record: dict, tool_wait_ms: int) -> None:
    record["tool_wait_ms"] = tool_wait_ms


def finish_step(record: dict) -> dict:
    """Set step_wall_ms from _t0, drop _t0, return the public record."""
    elapsed = int((time.monotonic() - record["_t0"]) * 1000)
    waits = int(record["model_wait_ms"]) + int(record["tool_wait_ms"])
    record["step_wall_ms"] = max(waits, elapsed)
    del record["_t0"]
    return record
