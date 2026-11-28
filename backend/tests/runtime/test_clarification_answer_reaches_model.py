"""An answered `ask_user` reaches the model when the parked run resumes.

A run asks, the person answers, the run is re-queued and runs again: the
model of the resumed leg must see the answer -- plain and structured alike."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.config import get_settings
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
from oc8.runtime.clarification import resolve_clarification
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_REGION = {
    "id": "r",
    "prompt": "Region?",
    "kind": "single",
    "options": [{"key": "eu", "label": "EU"}, {"key": "us", "label": "US"}],
}


class _AsksThenRecords:
    """Asks once; afterwards records what it is shown and finishes."""

    def __init__(self, ask: dict[str, Any]) -> None:
        self.ask = ask
        self.calls = 0
        self.seen: list[str] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        if self.calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id="c1", name="ask_user", arguments=self.ask)],
                usage=Usage(1, 1),
                stop_reason="tool_use",
                provider="ollama",
                model="m",
            )
        self.seen.append(
            "\n".join(
                part if isinstance(part, str) else str(part)
                for msg in req.messages
                for part in ([msg.content] if isinstance(msg.content, str) else msg.content or [])
            )
        )
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(1, 1),
            stop_reason="end_turn",
            provider="ollama",
            model="m",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _ask_answer_resume(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ask: dict[str, Any],
    answer: str,
    answers: dict[str, list[str]] | None = None,
) -> _AsksThenRecords:
    router = _AsksThenRecords(ask)
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    monkeypatch.setattr(get_settings(), "agent_isolation", False, raising=False)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Rep")
        db.add(agent)
        await db.flush()
        run = await RunRepository(db).create(
            tenant_id=tenant, agent_id=agent.id, context={"task": "plan the launch"}
        )
        run_id = run.id
    msg = RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False)
    await execute_run(msg)
    async with app_session(tenant) as db:
        parked = await db.get(m.AgentRun, run_id)
        assert parked is not None and parked.state == RunState.WAITING_FOR_INPUT.value
        await resolve_clarification(db, run=parked, answer=answer, answers=answers)
    await execute_run(msg)
    return router


async def test_a_plain_answer_reaches_the_resumed_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = await _ask_answer_resume(
        app_session, monkeypatch, ask={"question": "Which order number?"}, answer="SO-4711"
    )
    assert router.seen, "the resumed leg never called the model"
    assert "SO-4711" in router.seen[0]


async def test_a_structured_answer_reaches_the_resumed_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = await _ask_answer_resume(
        app_session, monkeypatch, ask={"questions": [_REGION]}, answer="", answers={"r": ["eu"]}
    )
    assert router.seen, "the resumed leg never called the model"
    assert "EU" in router.seen[0]
