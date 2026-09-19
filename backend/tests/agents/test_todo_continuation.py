# backend/tests/agents/test_todo_continuation.py
"""When a model tries to finish a run while its own todo_write list still has
open items, the harness must refuse and nudge it to keep going instead of
reporting "done" -- the Kai bug pattern this session started from: a status
report written with tickets still pending.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.harness.stages.d_todo import TODO_CONTINUATION_MAX_ROUNDS
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _LeavesOneTodoOpenThenFinishes:
    """Sets two todos, tries to finish with one still open, then -- once
    nudged -- marks it complete and finishes for real."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[Any]] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        self.seen_messages.append(req.messages)
        if self.calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="todo_write",
                        arguments={
                            "todos": [
                                {"content": "Resolve ticket A", "status": "completed"},
                                {"content": "Resolve ticket B", "status": "pending"},
                            ]
                        },
                    )
                ],
                usage=Usage(100, 20),
                stop_reason="tool_calls",
                provider="openrouter",
                model="z-ai/glm-5.3-flash",
            )
        if self.calls == 2:
            return CompletionResult(
                text="Ticket A is resolved.",
                tool_calls=[],
                usage=Usage(120, 20),
                stop_reason="stop",
                provider="openrouter",
                model="z-ai/glm-5.3-flash",
            )
        if self.calls == 3:
            return CompletionResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="todo_write",
                        arguments={
                            "todos": [
                                {"content": "Resolve ticket A", "status": "completed"},
                                {"content": "Resolve ticket B", "status": "completed"},
                            ]
                        },
                    )
                ],
                usage=Usage(130, 20),
                stop_reason="tool_calls",
                provider="openrouter",
                model="z-ai/glm-5.3-flash",
            )
        return CompletionResult(
            text="Both tickets resolved.",
            tool_calls=[],
            usage=Usage(140, 10),
            stop_reason="stop",
            provider="openrouter",
            model="z-ai/glm-5.3-flash",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


class _NeverClosesItsTodos:
    """Keeps trying to finish with an open todo forever -- the round cap must
    stop the nudging and let the run end anyway, never loop forever."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        if self.calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="todo_write",
                        arguments={"todos": [{"content": "Do the thing", "status": "pending"}]},
                    )
                ],
                usage=Usage(100, 20),
                stop_reason="tool_calls",
                provider="openrouter",
                model="z-ai/glm-5.3-flash",
            )
        return CompletionResult(
            text="I'm done.",
            tool_calls=[],
            usage=Usage(100, 10),
            stop_reason="stop",
            provider="openrouter",
            model="z-ai/glm-5.3-flash",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def test_finishing_with_an_open_todo_is_nudged_instead_of_accepted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _LeavesOneTodoOpenThenFinishes()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="IT-Support", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Kai")
        db.add(agent)
        await db.flush()
        result = await run_agent(db, agent=agent, task_text="Work the tickets", tenant_id=tenant)
    assert result.status == "done"
    assert result.output == "Both tickets resolved."
    assert all(t["status"] == "completed" for t in result.todos)
    assert router.calls == 4
    # The nudge (call 3's incoming messages) must name the still-open item.
    nudge_messages = router.seen_messages[2]
    assert any("Resolve ticket B" in str(msg.content) for msg in nudge_messages)


async def test_the_round_cap_stops_nudging_and_lets_the_run_end(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _NeverClosesItsTodos()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="IT-Support", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Kai")
        db.add(agent)
        await db.flush()
        result = await run_agent(db, agent=agent, task_text="Work the tickets", tenant_id=tenant)
    assert result.status == "done"
    # One call to set the todo, then one initial finish attempt plus exactly
    # TODO_CONTINUATION_MAX_ROUNDS nudged retries -- never unbounded.
    assert router.calls == 1 + 1 + TODO_CONTINUATION_MAX_ROUNDS
    assert any(t["status"] == "pending" for t in result.todos)
    # A run that gave up must not read like one that finished cleanly.
    assert "Do the thing" in result.output
    assert "still open" in result.output
