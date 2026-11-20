"""A gateway-driven run is stopped at its next tool call (§7.2).

A runtime that drives oc8 only through the MCP gateway has no `/step` oc8 can
answer with "stop". Its tool call is the step boundary: the same probe as the
isolated `/step` (`oc8.runtime.cancellation.run_stop`) runs there, in the same
order -- an operator's cancel, the tenant activity gate, then an ownership
change after an input was delivered -- and refuses the call before any tool
runs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.runtime.run_inputs import add_run_input, consume_run_inputs
from tests.api.test_mcp_gateway import (
    _FakeMcp,
    _no_session_leaks_between_tests,  # noqa: F401 -- the autouse fixture, applied here too
    _rpc,
    _seed,
    _token,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

CALL = {"name": "search_records", "arguments": {"model": "crm.lead"}}


async def _context(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID
) -> dict[str, Any]:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        return dict(run.context)


async def test_a_normal_run_calls_through(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/call", CALL)

    assert code == 200, body
    assert body["result"]["isError"] is False
    assert [c[0] for c in _FakeMcp.calls] == ["search_records"]
    assert "cancellation_kind" not in await _context(app_session, tenant, run_id)


async def test_an_operator_cancel_refuses_the_next_tool_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        db.add(
            m.RunCancellation(
                tenant_id=tenant,
                run_id=run_id,
                requested_at=dt.datetime.now(tz=dt.UTC),
                cancellation_kind="operator_interrupted",
            )
        )

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/call", CALL)

    assert code == 200, body
    assert body["result"]["isError"] is True
    assert "cancelled by operator" in body["result"]["content"][0]["text"]
    assert _FakeMcp.calls == []
    # The run_cancellation row is the record; nothing else is written.
    assert "cancellation_kind" not in await _context(app_session, tenant, run_id)


async def test_an_inactive_organisation_refuses_the_next_tool_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.tenants.activity import TENANT_INACTIVE_KIND, set_tenant_activity_gate

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/call", CALL)

    assert code == 200, body
    assert body["result"]["isError"] is True
    assert _FakeMcp.calls == []
    ctx = await _context(app_session, tenant, run_id)
    assert ctx["cancellation_kind"] == TENANT_INACTIVE_KIND


async def test_a_run_that_now_acts_for_someone_else_refuses_the_next_tool_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bob steered the agent's own schedule run and the run took his words;
    then Alice saved the schedule, so it acts through her account. Going on
    would act on Bob's words with her token: the call is refused instead and
    the run is stopped as `ownership_changed`, as `/step` stops it."""
    from oc8.runtime.run_inputs import OWNERSHIP_CHANGED_KIND, OWNERSHIP_CHANGED_OUTPUT

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        alice = m.OrgMember(
            tenant_id=tenant, subject="alice", subject_uuid=uuid.uuid4(), display_name="Alice"
        )
        bob = m.OrgMember(
            tenant_id=tenant, subject="bob", subject_uuid=uuid.uuid4(), display_name="Bob"
        )
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            task_text="daily mail digest",
            cron_expression="0 7 * * *",
            created_by_member_id=None,
        )
        db.add_all([alice, bob, trigger])
        await db.flush()
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.source = "cron"
        run.context = {**run.context, "trigger_id": str(trigger.id)}
        await db.flush()
        await add_run_input(db, run=run, member=bob, text="forward the mails")
        assert await consume_run_inputs(db, run_id=run.id, step=1)
        trigger.created_by_member_id = alice.id

    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/call", CALL)

    assert code == 200, body
    assert body["result"]["isError"] is True
    assert OWNERSHIP_CHANGED_OUTPUT in body["result"]["content"][0]["text"]
    assert _FakeMcp.calls == []
    ctx = await _context(app_session, tenant, run_id)
    assert ctx["cancellation_kind"] == OWNERSHIP_CHANGED_KIND


async def test_an_operator_cancel_wins_over_the_activity_gate(
    app_session: AppSessionFactory,
) -> None:
    """The probe's order is the executor's: when both hold, the run ends as the
    operator's cancel and nothing is recorded on the run context."""
    from oc8.runtime.cancellation import OPERATOR_CANCEL_OUTPUT, run_stop
    from oc8.tenants.activity import set_tenant_activity_gate

    tenant = uuid.uuid4()

    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    set_tenant_activity_gate(gate)
    async with app_session(tenant) as db:
        _agent_id, run_id, _t = await _seed(db, tenant)
        db.add(
            m.RunCancellation(
                tenant_id=tenant,
                run_id=run_id,
                requested_at=dt.datetime.now(tz=dt.UTC),
                cancellation_kind="operator_interrupted",
            )
        )
        await db.flush()
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        stop = await run_stop(db, run)
    assert stop is not None
    assert (stop.kind, stop.reason, stop.context_patch) == (
        "operator_interrupted",
        OPERATOR_CANCEL_OUTPUT,
        {},
    )
