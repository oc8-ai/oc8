"""In-process engine: a connection tool named like a core tool stays the connection's.

Routes always qualify such a tool (`build_routes`' `reserved`), even with a
single connection, so `<connection>.ask_user` reaches the connection's server
under the frame -- and the bare `ask_user` is only ever the core tool.
"""

from __future__ import annotations

import uuid

import pytest
from tests.agent.test_engine_parallel_reads import (
    _fixture,
    _RecordingServer,
    _ScriptedStream,
    _session_factory,
    _turn,
)
from tests.conftest import AppSessionFactory

from oc8.agent.engine import run_agent
from oc8.modelrouter import ToolCall

pytestmark = pytest.mark.asyncio


async def test_a_qualified_core_name_reaches_the_connection_and_does_not_park(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    recorder = _RecordingServer()
    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession", _session_factory(recorder, ["ask_user", "read_a"])
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn(
                    "",
                    ToolCall(id="c1", name="things.ask_user", arguments={"question": "?"}),
                ),
                _turn("Done."),
            ]
        ),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db, tenant, tool_scopes={"read": ["read_a", "ask_user"], "modify": []}
        )
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert result.status == "done", result
    assert [w[0] for w in recorder.windows] == ["ask_user"], "the connection's tool ran"


async def test_the_bare_core_name_still_parks_as_the_core_ask_user(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    recorder = _RecordingServer()
    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession", _session_factory(recorder, ["ask_user", "read_a"])
    )
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [_turn("", ToolCall(id="c1", name="ask_user", arguments={"question": "Which?"}))]
        ),
    )
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db, tenant, tool_scopes={"read": ["read_a", "ask_user"], "modify": []}
        )
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert result.status == "waiting_for_input", result
    assert recorder.windows == []
