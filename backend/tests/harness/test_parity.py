"""Spec §3.2 invariant 4 / §9.3: the in-process engine and the isolated
runtime's /step + /tool endpoints produce identical messages for identical
inputs. Same scripted model, same stub tool server, two fresh tenants (so the
department prompt cache cannot serve one run's answer to the other), and the
transcript after the preamble must match message for message.

The shared surface includes B0 authorize, B8 outward (no-target path), B9
write replay, C1/C2/C3/C5/C6 result shaping, and D1 todo continuation.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.harness.stages.c_spill import SPILL_THRESHOLD_CHARS
from oc8.agent.tool_semantics import record_identity as _real_record_identity
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

_BIG = "r" * (SPILL_THRESHOLD_CHARS + 50)
_FENCED_BIG = f'<external source="things:search_records">\n{_BIG}\n</external>'


def _turn(text: str, *calls: ToolCall) -> CompletionResult:
    return CompletionResult(
        text=text,
        tool_calls=list(calls),
        usage=Usage(tokens_in=1, tokens_out=1),
        stop_reason="tool_use" if calls else "stop",
        provider="fake",
        model="fake",
    )


def _read(call_id: str, *, justification: str = "") -> ToolCall:
    arguments = {"model": "thing", "limit": 5}
    if justification:
        arguments["justification"] = justification
    return ToolCall(id=call_id, name="search_records", arguments=arguments)


def _failing_read(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="search_records",
        arguments={"model": "thing", "fail": True},
    )


def _write(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="create_record",
        arguments={"model": "thing", "record_id": 1, "values": {"name": "same"}},
    )


def _idempotent_write(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="upsert_record",
        arguments={"model": "thing", "id": 7, "values": {"name": "same"}},
    )


def _record_identity(
    tool: str, arguments: dict[str, Any], focus_spec: dict[str, Any] | None
) -> tuple[str, str] | None:
    # `todo_write` is a successful control-tool call whose frame decision is
    # not ALLOW, so no gate verdict/tier exists. Giving it an identity ensures
    # both runtimes exercise the guarded note_access call site.
    if tool == "todo_write":
        return ("todo", "synthetic")
    return _real_record_identity(tool, arguments, focus_spec)


#: One model turn per completion, in order. Each runtime gets its own copy.
def _script() -> list[CompletionResult]:
    return [
        _turn("", _read("c1", justification="Needed for the summary")),
        _turn("", _read("c2")),
        _turn("", _read("c3")),
        _turn("", _write("c-write-1")),
        _turn("", _write("c-write-2")),
        _turn("", _idempotent_write("c-idempotent-1")),
        _turn("", _idempotent_write("c-idempotent-2")),
        _turn("", _failing_read("c-error")),
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
        _turn("No re-read needed."),
        _turn("Finished anyway."),
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
    """The connection's read and write tools, with dispatch counting."""

    write_calls = 0
    idempotent_write_calls = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.tools = [
            NeutralTool(
                name="search_records",
                description="search",
                parameters={"type": "object", "properties": {}},
            ),
            NeutralTool(
                name="create_record",
                description="create",
                parameters={"type": "object", "properties": {}},
            ),
            NeutralTool(
                name="upsert_record",
                description="upsert",
                parameters={"type": "object", "properties": {}},
                annotations={"idempotentHint": True},
            ),
        ]

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        assert "justification" not in arguments
        if arguments.get("fail"):
            raise ValueError("fixture tool failure")
        if name == "create_record":
            type(self).write_calls += 1
            return "created id=1"
        if name == "upsert_record":
            type(self).idempotent_write_calls += 1
            return "upserted id=7"
        return _BIG


async def _fixture(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.McpConnection]:
    dept = m.Department(
        tenant_id=tenant,
        name="Ops",
        frame={"tools": {"things": {"enabled": True, "read": True, "modify": True}}},
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
        config={
            "command": "x",
            "args": [],
            "read_before_write": False,
            "focus_spec": {
                "entity_field": "model",
                "id_fields": ["record_id"],
                "labels": {"thing": "Thing"},
            },
        },
        scopes={
            "read": ["search_records"],
            "modify": ["create_record", "upsert_record"],
        },
    )
    db.add(conn)
    await db.flush()
    return agent, conn


# C3's step stamp (`[step N/M · HH:MM tz]`, prepended by Harness.shape() to
# every tool result) carries the real wall clock. The two runtimes here run
# sequentially (in-process, then the isolated HTTP round trips), so a run
# straddling a real minute boundary would otherwise make an identical
# transcript compare unequal on the `HH:MM` alone -- masked the same way
# test_snapshot.py masks it, rather than freezing time.
_STEP_STAMP = re.compile(r"\[step \d+/\d+ · \d{2}:\d{2} [^\]]+\]")


def _normalise(messages: list[NeutralMessage]) -> list[tuple[str, Any, Any, Any]]:
    out: list[tuple[str, Any, Any, Any]] = []
    for msg in messages:
        calls = [(t.name, t.arguments) for t in msg.tool_calls] if msg.tool_calls else []
        content = msg.content
        if isinstance(content, str):
            content = _STEP_STAMP.sub("<step-stamp>", content)
        out.append((msg.role, content, msg.name, calls))
    return out


def _after_preamble(messages: list[NeutralMessage]) -> list[NeutralMessage]:
    first_assistant = next(i for i, msg in enumerate(messages) if msg.role == "assistant")
    return messages[first_assistant:]


async def _run_in_process(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[NeutralMessage], str]:
    tenant = uuid.uuid4()
    _StubSession.write_calls = 0
    _StubSession.idempotent_write_calls = 0
    stream = _ScriptedStream(_script())
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    monkeypatch.setattr("oc8.agent.engine.McpSession", _StubSession)
    monkeypatch.setattr("oc8.agent.engine.record_identity", _record_identity)
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant)
        result = await run_agent(
            db,
            agent=agent,
            task_text="Look things up and summarise.",
            tenant_id=tenant,
            mcp_conn=conn,
        )
        completion_status = await db.scalar(
            select(m.ActivityEvent.status)
            .where(
                m.ActivityEvent.agent_id == agent.id,
                m.ActivityEvent.message.like("% completed:%"),
            )
            .order_by(m.ActivityEvent.ts.desc())
            .limit(1)
        )
    assert result.status == "done", result
    assert completion_status == "warning"
    assert _StubSession.write_calls == 1, "the second in-process write must replay"
    assert _StubSession.idempotent_write_calls == 2, "idempotentHint must bypass replay"
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
    _StubSession.write_calls = 0
    _StubSession.idempotent_write_calls = 0
    stream = _ScriptedStream(_script())
    monkeypatch.setattr("oc8.api.v1.internal_agent.stream_completion_with_fallback", stream)
    monkeypatch.setattr("oc8.api.v1.internal_agent.McpSession", _StubSession)
    monkeypatch.setattr("oc8.api.v1.internal_agent.record_identity", _record_identity)
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
            for step_no in range(1, 21):
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
                    payload = r.json()
                    if call["name"] == "search_records" and not call["arguments"].get("fail"):
                        assert payload["spill"] == {
                            "filename": f"step-{step_no}-search_records.txt",
                            "content": _FENCED_BIG,
                        }
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
                    assert payload["status"] not in (
                        "waiting_for_approval",
                        "waiting_for_input",
                    ), payload
            else:
                raise AssertionError("the isolated run never finished")
    assert _StubSession.write_calls == 1, "the second isolated write must replay"
    assert _StubSession.idempotent_write_calls == 2, "idempotentHint must bypass replay"

    async with app_session(tenant) as db:
        run_row = await db.get(m.AgentRun, run_id)
        assert run_row is not None
        transcript = _to_messages(list(run_row.context["transcript"]))
        assert "harness" in run_row.context, "HarnessState must be persisted on run.context"
        assert "repeat_tracker" not in run_row.context
        assert "tool_output_chars" not in run_row.context
        assert not any(
            "older result chars masked" in str(message.content) for message in transcript
        ), "the persisted transcript must remain unmasked"
    # Compare the model-bound transcript from the final completion. /step
    # returns the exhausted note separately from the persisted raw model turn.
    transcript = [
        *stream.seen[-1],
        NeutralMessage(role="assistant", content=final_text, tool_calls=[]),
    ]
    return transcript, final_text


async def test_both_runtimes_produce_the_same_transcript(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, minio_url: str
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
    assert any("kept as file" in str(c) for c in tool_msgs), "C1 spill missing"
    assert any('<external source="things:create_record">' in str(c) for c in tool_msgs)
    assert any("NO second action was taken" in str(c) for c in tool_msgs), "B9 replay missing"
    errors = [str(c) for c in tool_msgs if "fixture tool failure" in str(c)]
    assert len(errors) == 1
    assert errors[0].startswith("<step-stamp> ERROR from things (search_records):")

    for transcript in (left, right):
        reminder_indexes = [
            index
            for index, (role, content, _, _) in enumerate(transcript)
            if role == "user" and "Re-read these" in str(content)
        ]
        assert len(reminder_indexes) == 2
        assert all(
            transcript[index - 1][0] == "assistant" for index in reminder_indexes
        ), "verify nudge must follow, rather than accept, the attempted finish"
        assert transcript[-1][0] == "assistant"

    assert in_process_text == isolated_text
    assert in_process_text.startswith("Finished anyway.")
    assert "were not re-verified" in in_process_text
