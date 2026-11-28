"""The in-process engine stops a run at its token limit and when the same
call keeps repeating, before spending another model call on it
(oc8.agent.run_limits). Fixture as in test_engine_step_timings.py."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.run_limits import NO_PROGRESS_OUTPUT, REPEAT_LIMIT, TOKEN_LIMIT_OUTPUT
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeToolset:
    def __init__(self) -> None:
        self.tools = [NeutralTool(name="fs_read", description="read a file", parameters={})]
        self.calls = 0

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls += 1
        return "same answer"


class _LoopingRouter:
    """Never answers: one tool call per step, forever."""

    def __init__(self, *, vary: bool, tokens_in: int) -> None:
        self.vary = vary
        self.tokens_in = tokens_in
        self.calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        args = {"path": f"/tmp/{self.calls}.txt"} if self.vary else {"path": "/tmp/a.txt"}
        return CompletionResult(
            text="",
            tool_calls=[ToolCall(id=f"c{self.calls}", name="fs_read", arguments=args)],
            usage=Usage(tokens_in=self.tokens_in, tokens_out=100),
            stop_reason="tool_use",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _agent(db: Any, tenant: uuid.UUID, definition: dict[str, Any]) -> m.Agent:
    dept = m.Department(
        tenant_id=tenant,
        name="Eng",
        frame={
            "tools": {
                "coding": {"enabled": True, "read": True, "modify": True, "approval_eur": None}
            }
        },
    )
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Coder", definition=definition)
    db.add(agent)
    await db.flush()
    return agent


async def test_a_run_repeating_the_same_call_stops_once_the_reminders_failed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _LoopingRouter(vary=False, tokens_in=10)
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    toolset = _FakeToolset()
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, {"max_steps": 50})
        result = await run_agent(
            db, agent=agent, task_text="read it", tenant_id=tenant, toolset=toolset
        )

    assert result.output == NO_PROGRESS_OUTPUT
    assert router.calls == REPEAT_LIMIT
    assert toolset.calls == REPEAT_LIMIT


async def test_a_run_stops_once_it_has_used_its_token_limit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _LoopingRouter(vary=True, tokens_in=30_000)
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, {"max_steps": 20, "max_run_tokens": 50_000})
        result = await run_agent(
            db, agent=agent, task_text="read them", tenant_id=tenant, toolset=_FakeToolset()
        )

    assert result.output == TOKEN_LIMIT_OUTPUT
    # 30,100 after the first step, 60,200 after the second: no third model call.
    assert router.calls == 2
