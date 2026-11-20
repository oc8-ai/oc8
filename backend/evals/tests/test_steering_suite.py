from __future__ import annotations

from pathlib import Path

from oc8_evals.tasks import load_suite, resolve_scenario

SUITES = Path(__file__).resolve().parents[1] / "oc8_evals" / "suites"


def test_the_steering_suite_loads_and_resolves() -> None:
    [task] = load_suite(SUITES / "steering")
    assert task.id == "steer_running_agent" and task.systems == ("odoo",)
    scenario = resolve_scenario(task.scenario)
    for hook in ("setup", "expect", "forbid", "teardown"):
        assert callable(getattr(scenario, hook))
