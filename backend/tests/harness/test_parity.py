"""Spec §3.2 invariant 4 / §9.3: the in-process engine and the isolated
runtime's /step + /tool endpoints produce identical messages for identical
inputs. Same scripted model, same stub tool server, two fresh tenants (so the
department prompt cache cannot serve one run's answer to the other), and the
transcript after the preamble must match message for message.

Scope in package 1 = exactly what both runtimes share today: B0 authorize,
B8 outward (no-target path), C5 cap/budget/repeat, D1 todo continuation. B9
idempotency is isolated-only today and is NOT compared here (spec §1.1).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.harness.stages.c_reminders import MAX_TOOL_RESULT_CHARS
from oc8.api.v1.internal_agent import _to_messages
from oc8.modelrouter import (
    CompletionResult,
    NeutralMessage,
    NeutralTool,
    ToolCall,
    Usage,
    chunk_from_result,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_BIG = "r" * (MAX_TOOL_RESULT_CHARS + 50)


def _turn(text: str, *calls: ToolCall) -> CompletionResult:
    return CompletionResult(
        text=text,
        tool_calls=list(calls),
        usage=Usage(tokens_in=1, tokens_out=1),
        stop_reason="tool_use" if calls else "stop",
        provider="fake",
        model="fake",
    )


def _read(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="search_records", arguments={"model": "thing", "limit": 5})


#: One model turn per completion, in order. Each runtime gets its own copy.
def _script() -> list[CompletionResult]:
    return [
        _turn("", _read("c1")),
        _turn("", _read("c2")),
        _turn("", _read("c3")),
        _turn(
            "",
            ToolCall(
                id="c4",
                name="todo_write",
                arguments={
                    "todos": [
                        {"content": "look things up", "status": "completed"},
                        {"content": "write the summary", "status": "pending"},
                    ]
                },
            ),
        ),
        _turn("All done."),
        _turn(
            "",
            ToolCall(
                id="c5",
                name="todo_write",
                arguments={
                    "todos": [
                        {"content": "look things up", "status": "completed"},
                        {"content": "write the summary", "status": "completed"},
                    ]
                },
            ),
        ),
        _turn("Summary written."),
    ]


class _ScriptedStream:
    """A `stream_completion_with_fallback` stand-in: pops the next scripted
    turn and records the messages it was asked to complete."""

    def __init__(self, script: list[CompletionResult]) -> None:
        self.script = script
        self.seen: list[list[NeutralMessage]] = []

    def __call__(self, *args: Any, **kw: Any) -> Any:
        self.seen.append(list(kw["messages"]))

        async def _gen() -> Any:
            yield chunk_from_result(self.script.pop(0))

        return _gen()


class _StubSession:
    """The connection's tool server: one read tool with an oversized result."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.tools = [
            NeutralTool(
                name="search_records",
                description="search",
                parameters={"type": "object", "properties": {}},
            )
        ]

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return _BIG


async def _fixture(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.McpConnection]:
    dept = m.Department(
        tenant_id=tenant,
        name="Ops",
        frame={"tools": {"things": {"enabled": True, "read": True, "modify": False}}},
    )
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="things",
        transport="stdio",
        server_url="stdio://things",
        connected=True,
        config={"command": "x", "args": []},
        scopes={"read": ["search_records"], "write": []},
    )
    db.add(conn)
    await db.flush()
    return agent, conn


def _normalise(messages: list[NeutralMessage]) -> list[tuple[str, Any, Any, Any]]:
    out: list[tuple[str, Any, Any, Any]] = []
    for msg in messages:
        calls = [(t.name, t.arguments) for t in msg.tool_calls] if msg.tool_calls else []
        out.append((msg.role, msg.content, msg.name, calls))
    return out


def _after_preamble(messages: list[NeutralMessage]) -> list[NeutralMessage]:
    first_assistant = next(i for i, msg in enumerate(messages) if msg.role == "assistant")
    return messages[first_assistant:]


async def _run_in_process(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[NeutralMessage], str]:
    tenant = uuid.uuid4()
    stream = _ScriptedStream(_script())
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    monkeypatch.setattr("oc8.agent.engine.McpSession", _StubSession)
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="Look things up and summarise.",
            tenant_id=tenant,
            mcp_conn=conn,
        )
    assert result.status == "done", result
    # The last completion saw everything up to (not including) its own answer;
    # append that answer so both sides end on the same final assistant turn.
    final = [
        *stream.seen[-1],
        NeutralMessage(role="assistant", content=result.output, tool_calls=[]),
    ]
    return final, result.output


async def _run_isolated(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[NeutralMessage], str]:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.auth import get_identity_provider
    from oc8.main import create_app
    from oc8.runtime.states import RunState

    tenant = uuid.uuid4()
    stream = _ScriptedStream(_script())
    monkeypatch.setattr("oc8.api.v1.internal_agent.stream_completion_with_fallback", stream)
    monkeypatch.setattr("oc8.api.v1.internal_agent.McpSession", _StubSession)
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant)
        task = m.Task(
            tenant_id=tenant,
            department_id=agent.department_id,
            assigned_agent_id=agent.id,
            title="Look things up and summarise.",
            state="in_progress",
        )
        db.add(task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            task_id=task.id,
            state=RunState.RUNNING.value,
            context={"task": "Look things up and summarise.", "mcp_connection_id": str(conn.id)},
        )
        db.add(run)
        await db.flush()
        agent_id, run_id = agent.id, run.id

    token = get_identity_provider().mint(
        tenant_id=tenant,
        subject=f"agent:{agent_id}",
        role="agent_default",
        kind="agent",
        scopes=[f"run:{run_id}"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app()
    final_text = ""
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # The shell's loop protocol (isolated_shell.py), inline.
            for _ in range(20):
                r = await c.post(f"/api/v1/internal/agent/{run_id}/step", headers=headers)
                assert r.status_code == 200, r.text
                step = r.json()
                if step["done"]:
                    final_text = step["text"]
                    break
                for call in step["tool_calls"]:
                    r = await c.post(
                        f"/api/v1/internal/agent/{run_id}/tool", json=call, headers=headers
                    )
                    assert r.status_code == 200, r.text
                    # Same gate as the real shell loop (isolated_shell.py): only
                    # a suspend verdict stops the run early. `todo_write` (and
                    # any other control tool the frame's own policy would deny
                    # as an ordinary connection call) is dispatched regardless
                    # of `decision.effect` -- see execute_control_tool -- so
                    # this endpoint reports it as "denied" even though it ran
                    # and its real output landed in the transcript. That status
                    # string is this endpoint's own bookkeeping, not something
                    # either runtime's transcript reflects, so it is not part
                    # of the parity being tested here.
                    assert r.json()["status"] not in (
                        "waiting_for_approval",
                        "waiting_for_input",
                    ), r.json()
            else:
                raise AssertionError("the isolated run never finished")

    async with app_session(tenant) as db:
        run_row = await db.get(m.AgentRun, run_id)
        assert run_row is not None
        transcript = _to_messages(list(run_row.context["transcript"]))
        assert "harness" in run_row.context, "HarnessState must be persisted on run.context"
        assert "repeat_tracker" not in run_row.context
        assert "tool_output_chars" not in run_row.context
    return transcript, final_text


async def test_both_runtimes_produce_the_same_transcript(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_process, in_process_text = await _run_in_process(app_session, monkeypatch)
    isolated, isolated_text = await _run_isolated(app_session, monkeypatch)

    left = _normalise(_after_preamble(in_process))
    right = _normalise(_after_preamble(isolated))
    assert left == right

    # And the script really exercised what package 1 moved.
    roles_and_heads = [(role, str(content)[:24]) for role, content, _, _ in left]
    assert ("user", "You are repeating the ex") in roles_and_heads, "C5 repeat nudge missing"
    assert ("user", "You indicated you are fi") in roles_and_heads, "D1 nudge missing"
    tool_msgs = [c for role, c, _, _ in left if role == "tool"]
    assert any("more characters omitted" in str(c) for c in tool_msgs), "C5 cap missing"

    assert in_process_text == isolated_text == "Summary written."
