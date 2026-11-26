"""The in-process engine binds its run to every tool call, so binary results
become this run's files and file arguments resolve against them
(oc8.agent.run_files). The binding itself is what is checked here; what the
MCP session does with it is tests/agents/test_mcp_run_files.py."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from tests.agent.test_engine_parallel_reads import _fixture, _ScriptedStream, _turn
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.run_files import RunFiles, current_run_files
from oc8.modelrouter import NeutralTool, ToolCall

pytestmark = pytest.mark.asyncio


class _Session:
    def __init__(self, seen: list[RunFiles | None]) -> None:
        self.seen = seen
        self.tools = [NeutralTool(name="get_a", description="", parameters={"type": "object"})]

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.seen.append(current_run_files())
        return "ok"


async def test_a_tool_call_of_a_run_is_bound_to_that_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    seen: list[RunFiles | None] = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", lambda *a, **k: _Session(seen))
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream([_turn("", ToolCall(id="c1", name="get_a", arguments={})), _turn("done")]),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": ["get_a"], "modify": []})
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            context={"chat_session_id": str(uuid.uuid4())},
        )
        db.add(run)
        await db.flush()
        await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn, run_id=run.id
        )

    [bound] = seen
    assert bound is not None
    assert (bound.tenant_id, bound.run_id, bound.agent_id) == (tenant, run.id, agent.id)
    assert str(bound.chat_session_id) == run.context["chat_session_id"]
    # Nothing leaks past the run.
    assert current_run_files() is None
