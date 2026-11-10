"""A connection's tool that shares a core tool's name stays the connection's.

`odoo.ask_user` names the CONNECTION's ask_user. The gateway rewrote it to
`ask_user` and then took the core branch: the run parked, with no frame grant
for odoo at all.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_a_routed_core_name_is_decided_by_the_frame_not_the_core_branch(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, frame={"tools": {}})
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "tool_routes": {"odoo.ask_user": ["odoo", "ask_user"]}}
        await db.flush()

    _code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "odoo.ask_user", "arguments": {"question": "Which order?"}},
    )

    assert body["result"]["isError"] is True
    assert "no grant" in body["result"]["content"][0]["text"]
    assert _FakeMcp.calls == []
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert "isolated_result" not in run.context, "the core ask_user must not park the run"
