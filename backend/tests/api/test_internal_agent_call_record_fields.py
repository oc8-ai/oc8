"""`step`, `state`, `connection` on the ISOLATED runtime's call records
(run step timeline, Task 5), including the parked call this runtime never
recorded at all.

Helpers (`_agent_token`, `_mcp_backed_run`, `_post_tool`) are imported from
`tests/api/test_internal_agent.py` rather than duplicated here, so they can't
drift from that file's own copies.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.runtime.step_record import REQUIRED_CALL_KEYS
from tests.api.test_internal_agent import _mcp_backed_run, _post_tool
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_a_dispatched_call_records_step_state_and_connection(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Session:
        tools: list[Any] = []

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            return "created id=1"

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _Session)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _mcp_backed_run(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "steps": 2}
        await db.commit()

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "create_record",
        {"model": "sale.order", "values": {"partner_id": 7}},
    )
    assert code == 200, body

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        entry = run.context["toolCalls"][-1]

    assert REQUIRED_CALL_KEYS <= set(entry)
    assert entry["state"] == "done"
    assert entry["step"] == 2
    assert entry["connection"] == "odoo"


async def test_a_denied_call_is_denied_and_carries_the_reason(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Never:
        tools: list[Any] = []

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            raise AssertionError("a denied call must never reach the tool server")

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _Never)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # modify=False -> the frame denies create_record outright.
        agent_id, run_id = await _mcp_backed_run(db, tenant, modify=False)
        await db.commit()

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "create_record",
        {"model": "sale.order", "values": {"partner_id": 7}},
    )
    assert code == 200, body
    assert body["status"] == "denied", body

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        entry = run.context["toolCalls"][-1]

    assert entry["state"] == "denied"
    assert "startedAt" not in entry
    assert entry["reason"]
