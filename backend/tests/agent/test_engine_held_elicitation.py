"""In-process engine: an answered elicitation is decided again before it runs.

A connection call that asked the operator a question is held on the run and
re-run with the answer on the next leg. That re-run is a NEW call -- the answer
is merged into its arguments -- so the PEP and the B gate decide it as they
would any call, and one they would hold for a human is parked for an approval
of the merged call.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from tests.agent.test_engine_parallel_reads import _fixture, _ScriptedStream, _turn
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.elicitation import ElicitationNeeded
from oc8.agent.engine import run_agent
from oc8.agent.harness.calls import call_sig
from oc8.modelrouter import NeutralTool, ToolCall

pytestmark = pytest.mark.asyncio

HELD = {"model": "sale.order"}


class _Server:
    """One fake tool server per connection command; records what ran where."""

    ran: list[tuple[str, str, dict[str, Any]]] = []
    tools_by_command: dict[str, list[str]] = {}
    asks: set[str] = set()

    def __init__(self, command: str = "", *a: Any, **kw: Any) -> None:
        self._command = command
        self.tools = [
            NeutralTool(name=n, description=n, parameters={"type": "object", "properties": {}})
            for n in type(self).tools_by_command.get(command, ["write_a"])
        ]

    async def __aenter__(self) -> _Server:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name in type(self).asks:
            raise ElicitationNeeded("Which customer?")
        type(self).ran.append((self._command, name, dict(arguments)))
        return "done"


def _reset(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
    _Server.ran = []
    _Server.tools_by_command = {}
    _Server.asks = set()
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _Server)
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", _ScriptedStream(script))


async def _resume(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: str,
    mode: str | None = None,
    autonomy: str = "default",
    approval_actions: list[str] | None = None,
    approved_original: bool = False,
) -> tuple[Any, list[Any], dict[str, Any]]:
    _reset(monkeypatch, [_turn("Done.")])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={"read": [], "modify": ["write_a"]},
            approval_actions=approval_actions,
            parallel_tool_calls=False,
            agent_definition={"autonomy": autonomy},
        )
        context: dict[str, Any] = {
            "pending_elicitation": {
                "connection": "things",
                "tool": "write_a",
                "arguments": HELD,
                "question": "Which customer?",
            },
            "clarifications": [{"question": "Which customer?", "answer": answer}],
        }
        if mode is not None:
            context["chat_mode"] = mode
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context=context)
        db.add(run)
        await db.flush()
        pre = (
            {call_sig(ToolCall(id="x", name="write_a", arguments=HELD)): "approve"}
            if approved_original
            else None
        )
        result = await run_agent(
            db,
            agent=agent,
            task_text="go",
            tenant_id=tenant,
            mcp_conn=conn,
            run_id=run.id,
            pre_decided=pre,
        )
        approvals = (
            (
                await db.execute(
                    select(m.ApprovalRequest).where(m.ApprovalRequest.tenant_id == tenant)
                )
            )
            .scalars()
            .all()
        )
        await db.flush()
        await db.refresh(run)
        return result, list(approvals), dict(run.context)


async def test_plan_does_not_run_an_answered_write(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, approvals, ctx = await _resume(app_session, monkeypatch, answer="ACME", mode="plan")
    assert _Server.ran == []
    assert approvals == []
    assert "pending_elicitation" not in ctx
    assert result.status == "done"


async def test_a_frame_that_holds_the_write_parks_the_merged_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, approvals, ctx = await _resume(
        app_session, monkeypatch, answer="ACME", approval_actions=["modify"]
    )
    merged = {**HELD, "elicitation_answer": "ACME"}
    assert _Server.ran == []
    assert result.status == "waiting_for_approval"
    assert [a.payload["arguments"] for a in approvals] == [merged]
    assert ctx["pending_elicitation"]["arguments"] == merged


async def test_an_approval_of_the_original_call_does_not_cover_the_answered_one(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, approvals, _ctx = await _resume(
        app_session, monkeypatch, answer="ACME", autonomy="conservative", approved_original=True
    )
    assert _Server.ran == []
    assert result.status == "waiting_for_approval"
    assert [a.payload["arguments"] for a in approvals] == [{**HELD, "elicitation_answer": "ACME"}]


async def test_an_approval_stands_when_the_answer_leaves_the_call_unchanged(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, approvals, ctx = await _resume(
        app_session,
        monkeypatch,
        answer='{"model": "sale.order"}',
        autonomy="conservative",
        approved_original=True,
    )
    assert _Server.ran == [("x", "write_a", HELD)]
    assert approvals == []
    assert "pending_elicitation" not in ctx
    assert result.status == "done"


async def test_a_question_from_a_second_connection_is_held_under_that_connection(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held call names the connection it ran on -- not the run's first one
    -- so the answer goes back to the system that asked."""
    _reset(
        monkeypatch,
        [_turn("", ToolCall(id="c1", name="write_b", arguments={"model": "x"}))],
    )
    _Server.tools_by_command = {"x": ["write_a"], "y": ["write_b"]}
    _Server.asks = {"write_b"}
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db, tenant, tool_scopes={"read": [], "modify": ["write_a"]}, parallel_tool_calls=False
        )
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {
            "tools": {
                **dept.frame["tools"],
                "other": {"enabled": True, "read": True, "modify": True},
            }
        }
        other = m.McpConnection(
            tenant_id=tenant,
            department_id=agent.department_id,
            name="other",
            transport="stdio",
            server_url="stdio://other",
            connected=True,
            config={"command": "y", "args": []},
            scopes={"read": [], "modify": ["write_b"]},
        )
        db.add(other)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={})
        db.add(run)
        await db.flush()
        result = await run_agent(
            db,
            agent=agent,
            task_text="go",
            tenant_id=tenant,
            mcp_conns=[conn, other],
            run_id=run.id,
        )
        await db.flush()
        await db.refresh(run)
        assert result.status == "waiting_for_input", result
        assert run.context["pending_elicitation"]["connection"] == "other"
