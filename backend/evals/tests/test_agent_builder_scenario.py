from __future__ import annotations

from pathlib import Path

import pytest
from oc8_evals.scenarios.copilot_agent_builder import (
    question_rounds,
)
from oc8_evals.scenarios.copilot_agent_builder import (
    writes_or_connection_calls_before_proposal as early_calls,
)
from oc8_evals.tasks import load_suite, resolve_scenario

SUITES = Path(__file__).resolve().parents[1] / "oc8_evals" / "suites"
SCOPES = {"read": ["list_invoices"], "modify": ["post_invoice"]}


def _calls(*names: str) -> list[dict[str, str]]:
    return [{"tool": n} for n in names]


def test_the_scenario_is_in_the_copilot_suite() -> None:
    tasks = {t.id: t for t in load_suite(SUITES / "copilot")}
    task = tasks["copilot_agent_builder"]
    assert "Rechnungsprüfer" in task.task_text
    scenario = resolve_scenario(task.scenario)
    for hook in ("setup", "expect", "forbid", "teardown"):
        assert callable(getattr(scenario, hook))


@pytest.mark.asyncio
async def test_the_scenario_is_not_runnable_yet() -> None:
    scenario = resolve_scenario("copilot_agent_builder")
    with pytest.raises(RuntimeError, match="not runnable"):
        await scenario.setup(None)  # type: ignore[arg-type]


def test_question_rounds_count_ask_user_calls() -> None:
    assert question_rounds(_calls("builder_options", "ask_user", "ask_user", "propose_agent")) == 2
    assert question_rounds(_calls("ask_user", "ask_user", "ask_user", "propose_agent")) == 3
    assert question_rounds([]) == 0


def test_a_writing_core_tool_before_the_proposal_is_caught() -> None:
    assert early_calls(_calls("memory_write", "propose_agent", "memory_write")) == ["memory_write"]


def test_the_modes_own_tools_do_not_count() -> None:
    assert early_calls(_calls("ask_user", "builder_options", "propose_agent")) == []


def test_propose_agent_itself_does_not_count() -> None:
    assert early_calls(_calls("propose_agent")) == []


def test_a_read_only_connection_call_does_not_count() -> None:
    assert early_calls(_calls("list_invoices", "propose_agent"), SCOPES) == []


def test_a_write_connection_call_counts() -> None:
    assert early_calls(_calls("post_invoice", "propose_agent"), SCOPES) == ["post_invoice"]


def test_an_unclassified_connection_call_counts_fail_closed() -> None:
    assert early_calls(_calls("mystery_tool")) == ["mystery_tool"]


def test_a_write_after_the_proposal_does_not_count() -> None:
    calls = _calls("ask_user", "propose_agent", "post_invoice", "memory_write")
    assert early_calls(calls, SCOPES) == []
