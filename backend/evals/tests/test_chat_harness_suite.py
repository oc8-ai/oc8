from __future__ import annotations

from pathlib import Path

import pytest
from oc8_evals.scoring import Attempt, Check, summarise, write_report
from oc8_evals.tasks import load_suite, load_task, resolve_scenario, starter

SUITES = Path(__file__).resolve().parents[1] / "oc8_evals" / "suites"

_CHAT = """
id = "a_chat"
kind = "chat"
group = "recall"
systems = []
origin = "manual"
scenario = "chat_recall"
task_text = "What was the code?"

[params]
early = ["The code is ZX-1."]
expect = ["ZX-1"]
"""


def test_a_chat_task_loads_its_kind_group_and_params(tmp_path: Path) -> None:
    p = tmp_path / "a_chat.toml"
    p.write_text(_CHAT)
    task = load_task(p)
    assert (task.kind, task.group) == ("chat", "recall")
    assert task.params == {"early": ["The code is ZX-1."], "expect": ["ZX-1"]}


def test_a_task_without_kind_is_a_run(tmp_path: Path) -> None:
    p = tmp_path / "a_chat.toml"
    p.write_text(_CHAT.replace('kind = "chat"\n', "").replace('group = "recall"\n', ""))
    task = load_task(p)
    assert (task.kind, task.group, task.params["expect"]) == ("run", "", ["ZX-1"])


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "a_chat.toml"
    p.write_text(_CHAT.replace('kind = "chat"', 'kind = "chatty"'))
    with pytest.raises(ValueError, match="kind"):
        load_task(p)


def test_every_chat_harness_case_resolves_its_scenario() -> None:
    tasks = load_suite(SUITES / "chat_harness")
    assert len(tasks) == 33
    for t in tasks:
        scenario = resolve_scenario(t.scenario)
        for hook in ("setup", "expect", "forbid", "teardown"):
            assert callable(getattr(scenario, hook)), f"{t.id}: scenario lacks {hook}"
    starts = {t.group for t in tasks if starter(resolve_scenario(t.scenario)) is not None}
    assert starts == {"rejection_reason", "edit_regenerate", "web"}


def _attempt(task: str, group: str, *, hit: float, cached: int) -> Attempt:
    return Attempt(
        task_id=task,
        attempt=1,
        run_id="r",
        final_state="done",
        steps=2,
        tool_calls=1,
        tokens_in=1000,
        tokens_out=100,
        platform_units=0,
        expect=[Check("answer", True)],
        forbid=[],
        asked=False,
        parked=False,
        duration_s=3.0,
        cache_read_tokens=cached,
        metrics={"hit@1": hit},
        group=group,
    )


def test_cached_tokens_and_metrics_are_reported_per_task_and_group(tmp_path: Path) -> None:
    attempts = [
        _attempt("k1", "knowledge", hit=1.0, cached=400),
        _attempt("k2", "knowledge", hit=0.0, cached=200),
    ]
    summaries = summarise(attempts, expects_question={})
    assert [s.metrics for s in summaries] == [{"hit@1": 1.0}, {"hit@1": 0.0}]
    assert [s.mean_cache_read_tokens for s in summaries] == [400.0, 200.0]
    write_report(
        tmp_path / "r.json", tmp_path / "r.md", meta={}, attempts=attempts, summaries=summaries
    )
    md = (tmp_path / "r.md").read_text()
    assert "| knowledge | 2 | 1.00 | 300.0 | hit@1=0.5 |" in md
