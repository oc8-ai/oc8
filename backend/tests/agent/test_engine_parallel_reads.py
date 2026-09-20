"""Parallel reads under caps (spec §3.5): when caps.parallel_tool_calls is
set, a turn's leading run of ALLOW + read-tier + non-control tool calls
dispatches concurrently instead of one at a time. Everything after the
first call that breaks that run (a write, a control tool, a non-ALLOW
decision) still runs sequentially through the existing per-call loop."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import (
    CompletionResult,
    NeutralTool,
    ToolCall,
    Usage,
    chunk_from_result,
)

pytestmark = pytest.mark.asyncio


def _turn(text: str, *calls: ToolCall) -> CompletionResult:
    return CompletionResult(
        text=text,
        tool_calls=list(calls),
        usage=Usage(tokens_in=1, tokens_out=1),
        stop_reason="tool_use" if calls else "stop",
        provider="fake",
        model="fake",
    )


class _ScriptedStream:
    """A `stream_completion_with_fallback` stand-in: pops the next scripted
    turn, same as tests/harness/test_parity.py's helper of the same name."""

    def __init__(self, script: list[CompletionResult]) -> None:
        self.script = script

    def __call__(self, *args: Any, **kw: Any) -> Any:
        async def _gen() -> Any:
            yield chunk_from_result(self.script.pop(0))

        return _gen()


class _RecordingServer:
    """Fake MCP server: records call start/end order and enforces that no
    two calls' [start, end) windows overlap unless dispatched concurrently."""

    def __init__(self, delay_s: float = 0.05) -> None:
        self.delay_s = delay_s
        self.windows: list[tuple[str, float, float]] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(self.delay_s)
        end = asyncio.get_event_loop().time()
        self.windows.append((name, start, end))
        return f"{name} result"

    def overlapping_pairs(self) -> int:
        count = 0
        for i, (_, s1, e1) in enumerate(self.windows):
            for _, s2, e2 in self.windows[i + 1 :]:
                if s1 < e2 and s2 < e1:
                    count += 1
        return count


class _StubSession:
    """The connection's tool server: a fixed tool list, calls proxied to a
    shared `_RecordingServer` so tests can inspect dispatch timing."""

    def __init__(self, recorder: _RecordingServer, tool_names: list[str]) -> None:
        self._recorder = recorder
        self.tools = [
            NeutralTool(name=n, description=n, parameters={"type": "object", "properties": {}})
            for n in tool_names
        ]

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return await self._recorder.call(name, arguments)


def _session_factory(recorder: _RecordingServer, tool_names: list[str]) -> Any:
    def _factory(*_args: Any, **_kwargs: Any) -> _StubSession:
        return _StubSession(recorder, tool_names)

    return _factory


async def _fixture(
    db: Any,
    tenant: uuid.UUID,
    *,
    tool_scopes: dict[str, list[str]],
    approval_actions: list[str] | None = None,
    parallel_tool_calls: bool = True,
    outward_tools: list[str] | None = None,
) -> tuple[m.Agent, m.McpConnection]:
    frame_policy: dict[str, Any] = {"enabled": True, "read": True, "modify": True}
    if approval_actions:
        frame_policy["approval_actions"] = approval_actions
    dept = m.Department(
        tenant_id=tenant,
        name="Ops",
        frame={"tools": {"things": frame_policy}},
    )
    db.add(dept)
    await db.flush()
    model_config = m.ModelConfig(
        tenant_id=tenant,
        provider="fake",
        model="fake",
        params={"parallel_tool_calls": parallel_tool_calls},
    )
    db.add(model_config)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        model_config_id=model_config.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    conn_config: dict[str, Any] = {"command": "x", "args": []}
    if outward_tools:
        conn_config["outward_tools"] = outward_tools
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="things",
        transport="stdio",
        server_url="stdio://things",
        connected=True,
        config=conn_config,
        scopes=tool_scopes,
    )
    db.add(conn)
    await db.flush()
    return agent, conn


async def test_leading_read_batch_dispatches_concurrently(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three read-tier ALLOW calls in one turn overlap in wall-clock time
    when caps.parallel_tool_calls is true."""
    tenant = uuid.uuid4()
    recorder = _RecordingServer()
    monkeypatch.setattr(
        "oc8.agent.engine.McpSession",
        _session_factory(recorder, ["read_a", "read_b", "read_c"]),
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn(
                    "",
                    ToolCall(id="c1", name="read_a", arguments={}),
                    ToolCall(id="c2", name="read_b", arguments={}),
                    ToolCall(id="c3", name="read_c", arguments={}),
                ),
                _turn("All done."),
            ]
        ),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={"read": ["read_a", "read_b", "read_c"], "modify": []},
        )
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn
        )

    assert result.status == "done", result
    assert recorder.overlapping_pairs() > 0
    assert [t["tool"] for t in result.tool_calls] == ["read_a", "read_b", "read_c"]


async def test_write_call_breaks_the_batch(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn of [read, read, write, read] only batches the leading two
    reads -- the write and everything after it dispatch sequentially, in
    order."""
    tenant = uuid.uuid4()
    recorder = _RecordingServer()
    monkeypatch.setattr(
        "oc8.agent.engine.McpSession",
        _session_factory(recorder, ["read_a", "read_b", "write_a", "read_c"]),
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn(
                    "",
                    ToolCall(id="c1", name="read_a", arguments={}),
                    ToolCall(id="c2", name="read_b", arguments={}),
                    ToolCall(id="c3", name="write_a", arguments={}),
                    ToolCall(id="c4", name="read_c", arguments={}),
                ),
                _turn("All done."),
            ]
        ),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={"read": ["read_a", "read_b", "read_c"], "modify": ["write_a"]},
        )
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn
        )

    assert result.status == "done", result
    # Only read_a/read_b (the leading run) overlap; write_a and read_c never
    # reach the pre-pass at all, so they cannot overlap with anything.
    assert recorder.overlapping_pairs() == 1
    assert [t["tool"] for t in result.tool_calls] == ["read_a", "read_b", "write_a", "read_c"]


async def test_outward_call_breaks_the_batch(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn of [read, read, outward-declared, read] only batches the
    leading two reads -- a call the connection declares outward (spec B8)
    breaks the run even though `required_right` would call it "read" too,
    same as a write does."""
    tenant = uuid.uuid4()
    recorder = _RecordingServer()
    monkeypatch.setattr(
        "oc8.agent.engine.McpSession",
        _session_factory(recorder, ["read_a", "read_b", "send_reply", "read_c"]),
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn(
                    "",
                    ToolCall(id="c1", name="read_a", arguments={}),
                    ToolCall(id="c2", name="read_b", arguments={}),
                    ToolCall(id="c3", name="send_reply", arguments={}),
                    ToolCall(id="c4", name="read_c", arguments={}),
                ),
                _turn("All done."),
            ]
        ),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={
                "read": ["read_a", "read_b", "read_c", "send_reply"],
                "modify": [],
            },
            outward_tools=["send_reply"],
        )
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn
        )

    assert result.status == "done", result
    # Only read_a/read_b (the leading run) overlap; send_reply is declared
    # outward on the connection, so it and read_c after it never enter the
    # batch -- both dispatch sequentially, in order.
    assert recorder.overlapping_pairs() == 1
    assert [t["tool"] for t in result.tool_calls] == ["read_a", "read_b", "send_reply", "read_c"]


async def test_approval_required_call_breaks_the_batch(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-tier call that requires approval (per a guardrail) is not
    batched even though required_right would call it "read" -- REQUIRE_
    APPROVAL is not ALLOW."""
    tenant = uuid.uuid4()
    recorder = _RecordingServer()
    monkeypatch.setattr(
        "oc8.agent.engine.McpSession",
        _session_factory(recorder, ["read_needs_approval", "read_ok"]),
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn(
                    "",
                    ToolCall(id="c1", name="read_needs_approval", arguments={}),
                    ToolCall(id="c2", name="read_ok", arguments={}),
                )
            ]
        ),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={"read": ["read_needs_approval", "read_ok"], "modify": []},
            approval_actions=["read_needs_approval"],
        )
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn
        )

    assert result.status == "waiting_for_approval", result
    # The call that needs approval never dispatches at all -- not batched,
    # not run sequentially either, until an operator decides it.
    assert recorder.windows == []


async def test_control_tool_never_batched(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control tool (todo_write) never enters the read batch even when its
    name is also granted "read" on the connection -- the exclusion is on the
    tool NAME being a control tool, not on its required_right classification."""
    tenant = uuid.uuid4()
    recorder = _RecordingServer()
    monkeypatch.setattr(
        "oc8.agent.engine.McpSession",
        _session_factory(recorder, ["read_a", "read_b", "read_c"]),
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn(
                    "",
                    ToolCall(id="c1", name="read_a", arguments={}),
                    ToolCall(id="c2", name="read_b", arguments={}),
                    ToolCall(
                        id="c3",
                        name="todo_write",
                        arguments={"todos": [{"content": "look things up", "status": "completed"}]},
                    ),
                    ToolCall(id="c4", name="read_c", arguments={}),
                ),
                _turn("All done."),
            ]
        ),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={
                "read": ["read_a", "read_b", "read_c", "todo_write"],
                "modify": [],
            },
        )
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn
        )

    assert result.status == "done", result
    # read_a/read_b overlap (the leading batch); todo_write never touches the
    # server at all, and read_c runs sequentially after it.
    assert recorder.overlapping_pairs() == 1
    assert [t["tool"] for t in result.tool_calls] == ["read_a", "read_b", "todo_write", "read_c"]
