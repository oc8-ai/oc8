"""A write the in-process engine holds names the run that raised it.

Every turn of an agent chat runs on the same task, so from the second turn on
"the run of this task" is ambiguous; without the run id in the payload the
decision could not be routed back to its run (E2E F6: 500
`MultipleResultsFound`, the run parked forever)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from tests.agent.test_engine_parallel_reads import (
    _fixture,
    _RecordingServer,
    _ScriptedStream,
    _StubSession,
    _turn,
)
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import ToolCall
from oc8.runtime.repository import RunRepository

pytestmark = pytest.mark.asyncio


async def test_an_in_process_held_write_carries_its_run_id(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer()

    async def _open(*_a: Any, **_kw: Any) -> _StubSession:
        return _StubSession(recorder, ["create_record"])

    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn("", ToolCall(id="c1", name="create_record", arguments={"id": 1})),
                _turn("Waiting for approval."),
            ]
        ),
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={"read": [], "modify": ["create_record"]},
            approval_actions=["create_record"],
        )
        run = await RunRepository(db).create(
            tenant_id=tenant, agent_id=agent.id, context={"task": "go"}
        )
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, run_id=run.id, mcp_conn=conn
        )
        approval = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.task_id == result.task_id)
            )
        ).scalar_one()
        run_id = run.id

    assert result.status == "waiting_for_approval"
    assert approval.payload["run_id"] == str(run_id)
