"""The worker's housekeeping as one public function, so every worker entrypoint
(Community's `oc8 worker`, and editions that wrap the run worker in their own
process) runs the same sweeps instead of each wiring its own list."""

from __future__ import annotations

from typing import Any

import pytest

from oc8.runtime import housekeeping


class _Driver:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def reap_orphans(self) -> int:
        self.calls.append("reap_orphans")
        return 0


def _patch_sweeps(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, failing: str = "") -> None:
    def fake(name: str) -> Any:
        async def _sweep(*_a: Any, **_k: Any) -> list[Any]:
            calls.append(name)
            if name == failing:
                raise RuntimeError("boom")
            return []

        return _sweep

    for name in (
        "close_abandoned_runs",
        "release_stale_running_agents",
        "recover_stranded_runs",
        "wake_cancelled_parked_runs",
        "sweep_evidence",
        "sweep_knowledge_deletions",
        "prune_expired_links",
        "prune_read_notifications",
        "prune_all_usage_counters",
        "purge_all_temporary_chats",
    ):
        monkeypatch.setattr(housekeeping, name, fake(name))
    monkeypatch.setattr(housekeeping, "get_sandbox_driver", lambda: _Driver(calls))


async def test_tick_runs_every_sweep_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_sweeps(monkeypatch, calls)

    await housekeeping.run_worker_housekeeping()

    assert calls == [
        "close_abandoned_runs",
        "release_stale_running_agents",
        "recover_stranded_runs",
        "wake_cancelled_parked_runs",
        "reap_orphans",
        "sweep_evidence",
        "sweep_knowledge_deletions",
        "prune_expired_links",
        "prune_read_notifications",
        "prune_all_usage_counters",
        "purge_all_temporary_chats",
    ]


async def test_one_failing_sweep_does_not_skip_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_sweeps(monkeypatch, calls, failing="sweep_evidence")

    await housekeeping.run_worker_housekeeping()

    assert calls[-5:] == [
        "sweep_knowledge_deletions",
        "prune_expired_links",
        "prune_read_notifications",
        "prune_all_usage_counters",
        "purge_all_temporary_chats",
    ]


async def test_startup_reaps_then_republishes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_sweeps(monkeypatch, calls)

    await housekeeping.run_worker_startup()

    assert calls == ["reap_orphans", "recover_stranded_runs"]
