"""A chat mode narrows a CONTAINERIZED run too (§5.2).

The whole reason this file exists next to the unit test: a tool list and a tool
authorization are built in three places, and a mode that only worked in the
in-process engine would do nothing for most real runs.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _run_in_mode(
    app_session: AppSessionFactory, tenant: uuid.UUID, mode: str | None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        if mode is not None:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            run.context = {**run.context, "chat_mode": mode}
            await db.flush()
    return agent_id, run_id


async def test_ask_advertises_no_tools_at_all(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    agent_id, run_id = await _run_in_mode(app_session, tenant, "ask")
    _code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    assert body["result"]["tools"] == []


async def test_plan_advertises_the_reads_and_withholds_the_writes(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    agent_id, run_id = await _run_in_mode(app_session, tenant, "plan")
    _code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    assert "search_records" in names
    assert "create_record" not in names
    assert "memory_write" not in names


async def test_a_withheld_call_is_refused_rather_than_forwarded(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness that cached an older tool list must be refused, not obeyed."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    agent_id, run_id = await _run_in_mode(app_session, tenant, "plan")
    _code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "sale.order", "values": {}}},
    )
    assert body["result"]["isError"] is True
    assert "/plan" in body["result"]["content"][0]["text"]
    assert _FakeMcp.calls == [], "nothing may reach the tool server"


async def test_gateway_memory_write_honours_the_mode(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness that cached an older tool list must be refused, not obeyed."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    agent_id, run_id = await _run_in_mode(app_session, tenant, "research_delegate")
    _code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "memory_write", "arguments": {"tier": "agent", "content": "x"}},
    )
    assert body["result"]["isError"] is True
    assert "research follow-up only reads" in body["result"]["content"][0]["text"]
    assert _FakeMcp.calls == [], "nothing may reach the tool server"


async def test_a_withheld_call_is_audited_as_a_denial(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    agent_id, run_id = await _run_in_mode(app_session, tenant, "plan")
    await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": {"model": "sale.order", "values": {}}},
    )
    async with app_session(tenant) as db:
        rows = (
            await db.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.tenant_id == tenant,
                    m.AuditEvent.action == "tool.call:create_record",
                )
            )
        ).scalars().all()
        assert [r.decision for r in rows] == ["deny"]
        assert "/plan" in (rows[0].reason or "")


async def test_no_mode_behaves_exactly_as_before(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    agent_id, run_id = await _run_in_mode(app_session, tenant, None)
    _code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    assert "create_record" in names


async def test_gateway_ask_user_honours_the_mode_and_does_not_park(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ask_user has its own branch that parks the run; a read-only research
    delegate calling it from a cached tool list must be refused before that."""
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    agent_id, run_id = await _run_in_mode(app_session, tenant, "research_delegate")
    _code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "ask_user", "arguments": {"question": "may I send the offer?"}},
    )
    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"]
    assert text.startswith("ERROR: ")
    assert "research follow-up only reads" in text
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert "isolated_result" not in run.context, "the run must not be parked"
