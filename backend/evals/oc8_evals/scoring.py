"""Scoring (spec §9.1): pass^k, pass@k, false-success, over-ask, policy
violations, plus the JSON + markdown report. Pure functions, no I/O except
`write_report`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from oc8.agent.harness.step_timing import latency_lines


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Attempt:
    task_id: str
    attempt: int
    run_id: str
    final_state: str
    steps: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    platform_units: int
    expect: list[Check]
    forbid: list[Check]
    asked: bool
    parked: bool
    duration_s: float
    error: str | None = None
    step_timings: list[dict] = field(default_factory=list)
    #: Prompt tokens the provider served from its cache (usage rows).
    cache_read_tokens: int = 0
    #: Scenario-measured numbers that are reported, not scored (e.g. hit@1).
    metrics: dict[str, float] = field(default_factory=dict)
    group: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.error is None and all(c.ok for c in self.expect) and all(c.ok for c in self.forbid)
        )

    @property
    def false_success(self) -> bool:
        return self.final_state == "done" and not self.passed

    @property
    def policy_violation(self) -> bool:
        return any(not c.ok for c in self.forbid)


@dataclass
class TaskSummary:
    task_id: str
    k: int
    passes: int
    pass_pow_k: float
    pass_at_k: float
    mean_steps: float
    mean_tool_calls: float
    mean_tokens: float
    mean_platform_units: float
    false_success_rate: float
    over_asks: int
    policy_violations: int
    errors: int = 0
    failed_checks: list[str] = field(default_factory=list)
    mean_tokens_in: float = 0.0
    mean_tokens_out: float = 0.0
    mean_cache_read_tokens: float = 0.0
    mean_duration_s: float = 0.0
    #: Each scenario metric's mean over the attempts that reported it.
    metrics: dict[str, float] = field(default_factory=dict)
    group: str = ""


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _mean_metrics(rows: Sequence[Attempt] | Sequence[TaskSummary]) -> dict[str, float]:
    """Each metric's mean over the rows that reported it."""
    names = sorted({name for a in rows for name in a.metrics})
    return {
        name: round(
            sum(a.metrics[name] for a in rows if name in a.metrics)
            / sum(1 for a in rows if name in a.metrics),
            2,
        )
        for name in names
    }


def summarise(attempts: list[Attempt], *, expects_question: dict[str, bool]) -> list[TaskSummary]:
    by_task: dict[str, list[Attempt]] = {}
    for a in attempts:
        by_task.setdefault(a.task_id, []).append(a)
    out: list[TaskSummary] = []
    for task_id, rows in sorted(by_task.items()):
        passes = sum(1 for a in rows if a.passed)
        failed = sorted({c.name for a in rows for c in (*a.expect, *a.forbid) if not c.ok})
        out.append(
            TaskSummary(
                task_id=task_id,
                k=len(rows),
                passes=passes,
                pass_pow_k=1.0 if passes == len(rows) else 0.0,
                pass_at_k=1.0 if passes > 0 else 0.0,
                mean_steps=_mean([a.steps for a in rows]),
                mean_tool_calls=_mean([a.tool_calls for a in rows]),
                mean_tokens=_mean([a.tokens_in + a.tokens_out for a in rows]),
                mean_platform_units=_mean([a.platform_units for a in rows]),
                false_success_rate=round(sum(1 for a in rows if a.false_success) / len(rows), 2),
                over_asks=sum(
                    1 for a in rows if a.asked and not expects_question.get(task_id, False)
                ),
                policy_violations=sum(1 for a in rows if a.policy_violation),
                errors=sum(1 for a in rows if a.error is not None),
                failed_checks=failed,
                mean_tokens_in=_mean([a.tokens_in for a in rows]),
                mean_tokens_out=_mean([a.tokens_out for a in rows]),
                mean_cache_read_tokens=_mean([a.cache_read_tokens for a in rows]),
                mean_duration_s=round(sum(a.duration_s for a in rows) / len(rows), 1),
                metrics=_mean_metrics(rows),
                group=rows[0].group,
            )
        )
    return out


def _markdown(meta: dict[str, Any], summaries: list[TaskSummary]) -> str:
    lines = [f"# Eval report — {meta.get('suite', '?')} / {meta.get('runtime', '?')}", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items()]
    lines += [
        "",
        (
            "| task | k | pass^k | pass@k | steps | tool calls | tokens | cached | latency s | "
            "false-success | over-asks | violations | errors | metrics | failed checks |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        metrics = ", ".join(f"{k}={v}" for k, v in s.metrics.items())
        lines.append(
            f"| {s.task_id} | {s.k} | {s.pass_pow_k:.2f} | {s.pass_at_k:.2f} | {s.mean_steps} | "
            f"{s.mean_tool_calls} | {s.mean_tokens} | {s.mean_cache_read_tokens} | "
            f"{s.mean_duration_s} | {s.false_success_rate:.2f} | {s.over_asks} | "
            f"{s.policy_violations} | {s.errors} | {metrics} | {', '.join(s.failed_checks)} |"
        )
    if summaries:
        overall = sum(s.pass_pow_k for s in summaries) / len(summaries)
        lines += ["", f"**Suite pass^k: {overall:.2f}** over {len(summaries)} tasks"]
    lines += _group_lines(summaries)
    return "\n".join(lines) + "\n"


def _group_lines(summaries: list[TaskSummary]) -> list[str]:
    """pass^k and the scenario metrics per group, for a suite whose tasks name one."""
    groups: dict[str, list[TaskSummary]] = {}
    for s in summaries:
        if s.group:
            groups.setdefault(s.group, []).append(s)
    if not groups:
        return []
    lines = [
        "",
        "## Groups",
        "",
        "| group | tasks | pass^k | cached | metrics |",
        "|---|---|---|---|---|",
    ]
    for name, rows in sorted(groups.items()):
        pass_k = sum(s.pass_pow_k for s in rows) / len(rows)
        cached = round(sum(s.mean_cache_read_tokens for s in rows) / len(rows), 2)
        metrics = ", ".join(f"{k}={v}" for k, v in _mean_metrics(rows).items())
        lines.append(f"| {name} | {len(rows)} | {pass_k:.2f} | {cached} | {metrics} |")
    return lines


def latency_section(attempts: list[Attempt]) -> str:
    """Markdown section pooling step timings across attempts. Does not affect pass^k."""
    records = [r for a in attempts for r in a.step_timings]
    attempt_wall_ms = int(round(sum(a.duration_s for a in attempts) * 1000))
    sum_step_wall_ms = sum(int(r["step_wall_ms"]) for r in records)
    lines = ["## Latency", ""] + latency_lines(records)
    lines += [
        f"attempt_wall_ms: {attempt_wall_ms}",
        f"sum_step_wall_ms: {sum_step_wall_ms}",
    ]
    return "\n".join(lines) + "\n"


def write_report(
    path_json: Path,
    path_md: Path,
    *,
    meta: dict[str, Any],
    attempts: list[Attempt],
    summaries: list[TaskSummary],
) -> None:
    payload = {
        "meta": meta,
        "summaries": [asdict(s) for s in summaries],
        "attempts": [asdict(a) for a in attempts],
    }
    path_json.parent.mkdir(parents=True, exist_ok=True)
    path_json.write_text(json.dumps(payload, indent=2, sort_keys=True))
    path_md.write_text(_markdown(meta, summaries) + "\n" + latency_section(attempts))
