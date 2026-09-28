"""Verifies dev's existing step-timing capture already satisfies the run step
timeline plan's needs (addendum to Tasks 1-3).

Tasks 1-3 originally asked to BUILD `stepTimings` capture from scratch. A
pre-flight investigation found dev had already shipped it
(`oc8.agent.harness.step_timing`'s `start_step`/`note_model`/`note_tools`/
`finish_step`, wired into both runtimes' own step loops for a Python
eval-latency CLI report) -- producing the same five fields this plan wants,
just snake_case rather than camelCase. This is the executable proof backing
that finding: a real two-step run through the in-process engine, asserting
the returned `step_timings` carry `step`/`model_wait_ms`/`ttft_ms`/
`tool_wait_ms`/`step_wall_ms` for every step. A future change to the harness
cannot silently regress this plan's data source without this test going red.

The fixture is lifted from tests/agents/test_engine_tool_call_timing.py: a
scripted router and a fake toolset, no sandbox, no provider. Asserted against
`RunResult.step_timings` (the return value), not a DB read of
`run.context["stepTimings"]` -- unlike `toolCalls`, the in-process engine
never writes `stepTimings` into the row itself; that happens once, wholesale,
in the executor's own terminal `merge_context` after `run_agent` returns
(see runtime/executor.py), exactly the same reason the existing
tool-call-timing tests in this file's neighbour assert on `result.tool_calls`
rather than the database.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeToolset:
    """Minimal `oc8.coding.tools.Toolset` -- no real sandbox needed, engine.py
    only ever calls `.tools` and `.call` on it."""

    def __init__(self, call: Callable[[str, dict[str, Any]], Awaitable[str]]) -> None:
        self.tools = [NeutralTool(name="fs_write", description="write a file", parameters={})]
        self._call = call

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return await self._call(name, arguments)


class _ScriptedRouter:
    """One fs_write call, then a final text with no tool calls -- two steps."""

    def __init__(self) -> None:
        self._calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self._calls += 1
        if self._calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id="c1", name="fs_write", arguments={"path": "/tmp/o.txt"})],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _coding_agent(db: Any, tenant: uuid.UUID) -> m.Agent:
    dept = m.Department(
        tenant_id=tenant,
        name="Eng",
        frame={
            "tools": {
                "coding": {
                    "enabled": True,
                    "read": True,
                    "modify": True,
                    "approval_eur": None,
                }
            }
        },
    )
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Coder")
    db.add(agent)
    await db.flush()
    return agent


async def test_a_two_step_run_records_one_timing_per_step_with_the_five_expected_keys(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _ScriptedRouter())

    async def _ok(name: str, arguments: dict[str, Any]) -> str:
        return "wrote it"

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _coding_agent(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            toolset=_FakeToolset(_ok),
        )

    assert [t["step"] for t in result.step_timings] == [1, 2]
    for entry in result.step_timings:
        assert set(entry) == {
            "step",
            "model_wait_ms",
            "ttft_ms",
            "tool_wait_ms",
            "step_wall_ms",
        }
        assert isinstance(entry["step_wall_ms"], int) and entry["step_wall_ms"] >= 0
        assert isinstance(entry["model_wait_ms"], int)


async def test_the_step_that_called_a_tool_carries_its_own_tool_wait(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 1 dispatches fs_write; step 2 only produces text. Equal
    `tool_wait_ms` on both would mean the accumulator is shared across steps
    instead of reset with each one."""
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _ScriptedRouter())

    async def _slow(name: str, arguments: dict[str, Any]) -> str:
        import asyncio

        await asyncio.sleep(0.03)
        return "wrote it"

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _coding_agent(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            toolset=_FakeToolset(_slow),
        )

    assert result.step_timings[0]["tool_wait_ms"] >= 30
    assert result.step_timings[1]["tool_wait_ms"] == 0


async def test_live_step_timings_publish_once_per_finished_step(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one genuinely missing piece this task adds: a live publish call
    alongside dev's existing finish_step capture, wired into the in-process
    engine at every point that already closes a step's timing."""
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _ScriptedRouter())

    published: list[dict[str, Any]] = []

    async def _fake_publish(
        tenant_id: uuid.UUID, *, run_id: uuid.UUID, timing: dict[str, Any]
    ) -> None:
        published.append(timing)

    monkeypatch.setattr("oc8.agent.engine.publish_run_step_timing", _fake_publish)

    async def _ok(name: str, arguments: dict[str, Any]) -> str:
        return "wrote it"

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _coding_agent(db, tenant)
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, state="running", context={"task": "x"}
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            run_id=run_id,
            toolset=_FakeToolset(_ok),
        )

    assert [t["step"] for t in published] == [1, 2]
    assert published == result.step_timings


async def test_a_run_without_a_run_id_publishes_nothing_and_does_not_fail(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_agent is called directly, with no run row, by several existing
    tests. The live-publish wiring must be a no-op there rather than an
    AttributeError that breaks every one of them."""
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _ScriptedRouter())

    async def _boom(*args: object, **kw: object) -> None:
        raise AssertionError("must not publish for a run_agent call with no run_id")

    monkeypatch.setattr("oc8.agent.engine.publish_run_step_timing", _boom)

    async def _ok(name: str, arguments: dict[str, Any]) -> str:
        return "wrote it"

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _coding_agent(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            toolset=_FakeToolset(_ok),
        )

    assert result.status == "done"
