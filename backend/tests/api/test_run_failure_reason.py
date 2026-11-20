"""`RunDTO.failure_reason`: why a failed run failed, instead of just "failed".

A runtime that gives up on its own records a sentence as the run's `output`;
an exception that escaped the runtime is stored as `error`, in repr form.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.api.v1.run import failure_reason, run_to_dto
from oc8.runtime.step_record import step_timing_dto


def _run(state: str, context: dict[str, Any]) -> m.AgentRun:
    return m.AgentRun(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        state=state,
        context=context,
        updated_at=dt.datetime.now(tz=dt.UTC),
    )


def test_the_runtimes_own_sentence_is_the_reason() -> None:
    run = _run("failed", {"output": "The model used its whole answer budget ...", "error": "x"})
    assert failure_reason(run) == "The model used its whole answer budget ..."


def test_an_exception_repr_is_cut_down_to_its_message() -> None:
    run = _run("failed", {"error": "RuntimeError('provider said no')"})
    assert failure_reason(run) == "provider said no"


def test_a_plain_error_string_is_kept() -> None:
    run = _run("failed", {"error": "lease lost: worker died mid-run"})
    assert failure_reason(run) == "lease lost: worker died mid-run"


def test_a_failed_run_without_any_reason_has_none() -> None:
    assert failure_reason(_run("failed", {"output": "  "})) is None


def test_only_failed_runs_have_a_reason() -> None:
    assert failure_reason(_run("done", {"output": "All done."})) is None


def test_run_to_dto_carries_the_reason() -> None:
    dto = run_to_dto(_run("failed", {"error": 'ValueError("bad")'}))
    assert dto.failure_reason == "bad"
    assert dto.model_dump(by_alias=True)["failureReason"] == "bad"


def test_step_dto_carries_budget_and_reasoning_only_when_recorded() -> None:
    old = {"step": 1, "model_wait_ms": 1, "ttft_ms": None, "tool_wait_ms": 0, "step_wall_ms": 2}
    assert set(step_timing_dto(old)) == {
        "step",
        "modelWaitMs",
        "ttftMs",
        "toolWaitMs",
        "stepWallMs",
    }
    new = {
        **old,
        "tokens_in": 100,
        "tokens_out": 4096,
        "reasoning_tokens": 4000,
        "max_tokens": 4096,
        "reasoning": "thought",
    }
    dto = step_timing_dto(new)
    assert dto["tokensIn"] == 100
    assert dto["tokensOut"] == 4096
    assert dto["reasoningTokens"] == 4000
    assert dto["maxTokens"] == 4096
    assert dto["reasoning"] == "thought"


@pytest.mark.asyncio
async def test_the_live_reasoning_event_names_its_run_and_step(monkeypatch: Any) -> None:
    import oc8.realtime.emit as emit

    published: list[tuple[str, dict[str, Any]]] = []

    class _Bus:
        async def publish_event(self, tenant_id: Any, type_: str, data: Any, **kw: Any) -> None:
            published.append((type_, data))

    monkeypatch.setattr(emit, "get_event_bus", lambda: _Bus())
    run_id = uuid.uuid4()
    await emit.publish_run_reasoning_delta(uuid.uuid4(), run_id=run_id, step=3, text="hmm")
    await emit.publish_run_reasoning_delta(uuid.uuid4(), run_id=run_id, step=3, text="")
    assert published == [("run.reasoning_delta", {"run_id": str(run_id), "step": 3, "text": "hmm"})]
