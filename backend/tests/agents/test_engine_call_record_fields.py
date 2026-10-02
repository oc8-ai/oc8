"""`step`, `state` and `connection` on every in-process call record
(run step timeline, Task 4).

The timeline joins calls to timings by `step`, groups rows by `connection` and
picks its glyph from `state`. All three are recorded, never inferred in the
client -- a browser reconstructing "did this fail or was it refused" from an
`ERROR:` prefix gets a guardrail denial wrong every time.

Fixtures mirror tests/agents/test_engine_tool_call_timing.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.authz.pdp import Decision, Effect
from oc8.capas.claude_hooks.runner import DispatchResult
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result
from oc8.runtime.step_record import CALL_STATES, REQUIRED_CALL_KEYS
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeToolset:
    def __init__(self, call: Callable[[str, dict[str, Any]], Awaitable[str]]) -> None:
        self.tools = [NeutralTool(name="fs_write", description="write a file", parameters={})]
        self._call = call

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return await self._call(name, arguments)


class _ScriptedRouter:
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


async def _run_once(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    tool: Callable[[str, dict[str, Any]], Awaitable[str]],
) -> list[dict[str, Any]]:
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _ScriptedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _coding_agent(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            toolset=_FakeToolset(tool),
        )
    return result.tool_calls


async def test_a_successful_call_is_done_and_names_its_step_and_connection(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(name: str, arguments: dict[str, Any]) -> str:
        return "wrote it"

    entry = (await _run_once(app_session, monkeypatch, _ok))[-1]
    assert REQUIRED_CALL_KEYS <= set(entry)
    assert entry["state"] == "done"
    assert entry["step"] == 1
    # The coding toolset's frame key, not an MCP connection name -- the
    # timeline groups by whatever this says, and "coding" is the honest label
    # for a sandbox tool.
    assert entry["connection"] == "coding"


async def test_a_tool_that_raised_is_failed_not_denied(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("disk full")

    entry = (await _run_once(app_session, monkeypatch, _boom))[-1]
    assert entry["state"] == "failed"
    # Not `.startswith("ERROR:")`: harness.shape()'s C3 step stamp prepends
    # "[step N/max · ...]" to every shaped result (see
    # oc8.agent.harness.prompts.format_step_stamp), so the model-facing text
    # reads "... ERROR from oc8 (fs_write): ..." rather than a literal
    # "ERROR:" prefix. `state` -- not the text of `result` -- is what the
    # timeline reads to tell a failure from a success.
    assert "ERROR" in entry["result"]


async def test_a_guardrail_denial_is_denied_not_failed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user-facing difference this field exists for: a refusal is not a
    breakage, and the output text alone cannot tell them apart -- both read
    `ERROR: ...`."""
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _ScriptedRouter())
    monkeypatch.setattr(
        "oc8.agent.engine._authorize",
        lambda *a, **k: Decision(Effect.DENY, "tool not allowed by the department frame"),
    )

    async def _never(name: str, arguments: dict[str, Any]) -> str:
        raise AssertionError("a denied call must never be dispatched")

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _coding_agent(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            toolset=_FakeToolset(_never),
        )

    entry = result.tool_calls[-1]
    assert entry["state"] == "denied"
    assert entry["reason"] == "tool not allowed by the department frame"


async def test_a_blocking_plugin_hook_is_denied(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _ScriptedRouter())

    async def _blocking(
        tenant_id: uuid.UUID, event: str, payload: dict[str, Any], *, tool_name: str | None = None
    ) -> DispatchResult:
        if event == "PreToolUse":
            return DispatchResult(blocked=True, reason="blocked by guardrail")
        return DispatchResult()

    monkeypatch.setattr("oc8.agent.engine.dispatch_claude_event", _blocking)

    async def _never(name: str, arguments: dict[str, Any]) -> str:
        raise AssertionError("a blocked call must never be dispatched")

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _coding_agent(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="write the file",
            tenant_id=tenant,
            toolset=_FakeToolset(_never),
        )

    entry = result.tool_calls[-1]
    assert entry["state"] == "denied"
    assert entry["step"] == 1


async def test_every_recorded_state_is_a_known_state(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(name: str, arguments: dict[str, Any]) -> str:
        return "wrote it"

    for entry in await _run_once(app_session, monkeypatch, _ok):
        assert entry["state"] in CALL_STATES
