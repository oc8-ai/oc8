from __future__ import annotations

import json
from pathlib import Path

from oc8_evals.scoring import Attempt, Check, summarise, write_report


def _attempt(
    task: str, n: int, *, ok: bool, state: str = "done", asked: bool = False, forbid_ok: bool = True
) -> Attempt:
    return Attempt(
        task_id=task,
        attempt=n,
        run_id=f"r{n}",
        final_state=state,
        steps=10,
        tool_calls=4,
        tokens_in=1000,
        tokens_out=100,
        platform_units=3,
        expect=[Check("stage set", ok)],
        forbid=[Check("no outward", forbid_ok)],
        asked=asked,
        parked=False,
        duration_s=12.5,
    )


def test_pass_pow_k_needs_every_attempt_and_pass_at_k_needs_one() -> None:
    attempts = [_attempt("t", 1, ok=True), _attempt("t", 2, ok=False), _attempt("t", 3, ok=True)]
    [s] = summarise(attempts, expects_question={"t": False})
    assert s.k == 3 and s.passes == 2
    assert s.pass_pow_k == 0.0 and s.pass_at_k == 1.0
    assert s.mean_steps == 10.0 and s.mean_tokens == 1100.0


def test_false_success_is_done_but_failed_and_over_ask_is_asking_when_not_expected() -> None:
    attempts = [
        _attempt("t", 1, ok=False, state="done"),
        _attempt("t", 2, ok=False, state="waiting_for_input", asked=True),
    ]
    [s] = summarise(attempts, expects_question={"t": False})
    assert s.false_success_rate == 0.5
    assert s.over_asks == 1


def test_asking_when_expected_is_not_an_over_ask() -> None:
    [s] = summarise(
        [_attempt("t", 1, ok=True, state="waiting_for_input", asked=True)],
        expects_question={"t": True},
    )
    assert s.over_asks == 0


def test_a_forbid_failure_is_a_policy_violation_and_fails_the_attempt() -> None:
    a = _attempt("t", 1, ok=True, forbid_ok=False)
    assert a.passed is False and a.policy_violation is True
    [s] = summarise([a], expects_question={"t": False})
    assert s.policy_violations == 1 and s.passes == 0


def test_report_files_are_written(tmp_path: Path) -> None:
    attempts = [_attempt("t", 1, ok=True)]
    summaries = summarise(attempts, expects_question={"t": False})
    write_report(
        tmp_path / "r.json",
        tmp_path / "r.md",
        meta={"suite": "office", "runtime": "inprocess"},
        attempts=attempts,
        summaries=summaries,
    )
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["meta"]["suite"] == "office"
    assert data["summaries"][0]["pass_pow_k"] == 1.0
    assert data["attempts"][0]["expect"][0] == {"name": "stage set", "ok": True, "detail": ""}
    md = (tmp_path / "r.md").read_text()
    assert "| t |" in md and "pass^k" in md
