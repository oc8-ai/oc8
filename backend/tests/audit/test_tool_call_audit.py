"""Tool calls in the audit log name their run and chat, and a dispatched call
records its outcome (spec §5.7) -- in every runtime."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from tests.agent.test_engine_parallel_reads import (
    _fixture,
    _RecordingServer,
    _ScriptedStream,
    _session_factory,
    _turn,
)
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.audit import verify_chain
from oc8.modelrouter import ToolCall

pytestmark = pytest.mark.asyncio


class _SecretServer(_RecordingServer):
    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        await super().call(name, arguments)
        return "SECRET-ROW-42"


async def _tool_events(
    app_session: AppSessionFactory, tenant: uuid.UUID, prefix: str
) -> list[m.AuditEvent]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.AuditEvent)
                    .where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action.startswith(prefix),
                    )
                    .order_by(m.AuditEvent.seq)
                )
            )
            .scalars()
            .all()
        )


async def _run(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    script: list[Any],
    *,
    recorder: _RecordingServer | None = None,
    chat: bool = False,
    with_run: bool = True,
    parallel: bool = False,
    approval_actions: list[str] | None = None,
    read_only: bool = False,
) -> tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None, str]:
    """Run the in-process engine on `things` (reads read_a/read_b, write
    create_record). Returns (tenant, run_id, chat_session_id, status)."""
    tenant = uuid.uuid4()
    recorder = recorder or _RecordingServer(delay_s=0.0)
    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession",
        _session_factory(recorder, ["read_a", "read_b", "create_record"]),
    )
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", _ScriptedStream(script))
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db,
            tenant,
            tool_scopes={"read": ["read_a", "read_b"], "modify": ["create_record"]},
            approval_actions=approval_actions,
            parallel_tool_calls=parallel,
        )
        if read_only:
            dept = await db.get(m.Department, agent.department_id)
            assert dept is not None
            dept.frame = {"tools": {"things": {"enabled": True, "read": True, "modify": False}}}
            await db.flush()
        run_id: uuid.UUID | None = None
        session_id: uuid.UUID | None = None
        if with_run:
            context: dict[str, Any] = {"task": "go"}
            if chat:
                session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
                db.add(session)
                await db.flush()
                session_id = session.id
                context["chat_session_id"] = str(session.id)
            run = m.AgentRun(
                tenant_id=tenant,
                agent_id=agent.id,
                state="running",
                source="chat" if chat else "cron",
                context=context,
            )
            db.add(run)
            await db.flush()
            run_id = run.id
        result = await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn, run_id=run_id
        )
    return tenant, run_id, session_id, result.status


def _one_read(name: str = "read_a") -> list[Any]:
    return [_turn("", ToolCall(id="c1", name=name, arguments={})), _turn("Done.")]


@pytest.mark.parametrize("chat", [True, False])
async def test_tool_call_event_names_run_and_chat(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, chat: bool
) -> None:
    tenant, run_id, session_id, status = await _run(
        app_session, monkeypatch, _one_read(), chat=chat
    )
    assert status == "done"
    (call,) = await _tool_events(app_session, tenant, "tool.call:read_a")
    assert call.resource["run_id"] == str(run_id)
    assert call.resource["tool"] == "read_a"
    assert "arguments" in call.resource
    if chat:
        assert call.resource["chat_session_id"] == str(session_id)
    else:
        assert "chat_session_id" not in call.resource


@pytest.mark.parametrize("parallel", [False, True])
async def test_a_dispatched_call_writes_one_result_event(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, parallel: bool
) -> None:
    """One ok and one failing call, sequentially and through the parallel
    read batch."""
    tenant, run_id, session_id, status = await _run(
        app_session,
        monkeypatch,
        [
            _turn(
                "",
                ToolCall(id="c1", name="read_a", arguments={}),
                ToolCall(id="c2", name="read_b", arguments={}),
            ),
            _turn("Done."),
        ],
        recorder=_RecordingServer(delay_s=0.0, failures={"read_b"}),
        chat=True,
        parallel=parallel,
    )
    assert status == "done"
    results = await _tool_events(app_session, tenant, "tool.result:")
    assert [r.action for r in results] == ["tool.result:read_a", "tool.result:read_b"]
    ok, failed = results
    assert ok.resource["outcome"] == "ok"
    assert failed.resource["outcome"] == "error"
    for event in results:
        assert event.category == "tool_action"
        assert event.decision == "allow"
        assert event.resource["run_id"] == str(run_id)
        assert event.resource["chat_session_id"] == str(session_id)
        assert event.resource["connection"] == "things"
        assert isinstance(event.resource["duration_ms"], int)
        assert event.resource["duration_ms"] >= 0
        assert event.responsible_type == "agent"


async def test_a_denied_or_parked_call_writes_no_result_event(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Parked: create_record needs an approval.
    tenant, _run_id, _s, status = await _run(
        app_session,
        monkeypatch,
        [
            _turn("", ToolCall(id="c1", name="create_record", arguments={"values": {}})),
            _turn("Waiting."),
        ],
        approval_actions=["create_record"],
    )
    assert status == "waiting_for_approval"
    calls = await _tool_events(app_session, tenant, "tool.call:create_record")
    assert [c.decision for c in calls] == ["require_approval"]
    assert await _tool_events(app_session, tenant, "tool.result:") == []

    # Denied: a write the department frame does not allow.
    tenant, _run_id, _s, status = await _run(
        app_session,
        monkeypatch,
        [
            _turn("", ToolCall(id="c1", name="create_record", arguments={"values": {}})),
            _turn("Could not."),
        ],
        read_only=True,
    )
    assert status == "done"
    calls = await _tool_events(app_session, tenant, "tool.call:create_record")
    assert [c.decision for c in calls] == ["deny"]
    assert await _tool_events(app_session, tenant, "tool.result:") == []


async def test_the_result_event_holds_no_result_content(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _run_id, _s, status = await _run(
        app_session, monkeypatch, _one_read(), recorder=_SecretServer(delay_s=0.0), chat=True
    )
    assert status == "done"
    (event,) = await _tool_events(app_session, tenant, "tool.result:read_a")
    assert set(event.resource) == {
        "tool",
        "run_id",
        "chat_session_id",
        "connection",
        "outcome",
        "duration_ms",
    }
    assert "SECRET-ROW-42" not in json.dumps(event.resource)
    assert "SECRET-ROW-42" not in (event.reason or "")


async def test_the_audit_chain_still_verifies(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _run_id, _s, _status = await _run(
        app_session,
        monkeypatch,
        [
            _turn(
                "",
                ToolCall(id="c1", name="read_a", arguments={}),
                ToolCall(id="c2", name="read_b", arguments={}),
            ),
            _turn("Done."),
        ],
        chat=True,
        parallel=True,
    )
    assert len(await _tool_events(app_session, tenant, "tool.result:")) == 2
    async with app_session(tenant) as db:
        assert await verify_chain(db, tenant)


async def test_a_call_outside_any_run_still_audits_its_result(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _run_id, _s, status = await _run(app_session, monkeypatch, _one_read(), with_run=False)
    assert status == "done"
    (call,) = await _tool_events(app_session, tenant, "tool.call:read_a")
    assert "run_id" not in call.resource
    (result,) = await _tool_events(app_session, tenant, "tool.result:read_a")
    assert result.resource["outcome"] == "ok"
    assert "run_id" not in result.resource


async def test_isolated_and_gateway_calls_are_audited_the_same_way(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.api.test_internal_agent import _mcp_backed_run, _post_tool
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token

    from oc8.agent import mcp_pool

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    _FakeMcp.calls = []
    session_id = uuid.uuid4()

    # Isolated control plane: POST /internal/agent/{run}/tool.
    isolated = uuid.uuid4()
    async with app_session(isolated) as db:
        agent_id, run_id = await _mcp_backed_run(db, isolated)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "chat_session_id": str(session_id)}
        await db.flush()
    code, body = await _post_tool(isolated, agent_id, run_id, "search_records", {"model": "x"})
    assert code == 200, body
    await mcp_pool.close_all()

    # MCP gateway: tools/call.
    gateway = uuid.uuid4()
    async with app_session(gateway) as db:
        g_agent, g_run, _task = await _seed(db, gateway)
    code, body = await _rpc(
        _token(gateway, g_agent, g_run),
        "tools/call",
        {"name": "search_records", "arguments": {"model": "crm.lead"}},
    )
    assert code == 200, body
    assert body["result"]["isError"] is False
    await mcp_pool.close_all()

    for tenant, rid, chat_id in ((isolated, run_id, session_id), (gateway, g_run, None)):
        (call,) = await _tool_events(app_session, tenant, "tool.call:search_records")
        assert call.decision == "allow"
        assert call.resource["run_id"] == str(rid)
        assert call.resource.get("chat_session_id") == (str(chat_id) if chat_id else None)
        (result,) = await _tool_events(app_session, tenant, "tool.result:search_records")
        assert result.decision == "allow"
        assert result.resource["outcome"] == "ok"
        assert result.resource["connection"] == "odoo"
        assert result.resource["run_id"] == str(rid)
        assert result.resource.get("chat_session_id") == (str(chat_id) if chat_id else None)
        assert result.resource["duration_ms"] >= 0
        assert "ok:search_records" not in json.dumps(result.resource)
        async with app_session(tenant) as db:
            assert await verify_chain(db, tenant)


async def test_isolated_and_gateway_failing_and_denied_calls(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing dispatched call is an `error` result; a denied one has none."""
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token

    from oc8.agent import mcp_pool

    class _Failing(_FakeMcp):
        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            raise RuntimeError("SECRET-ROW-42 unreachable")

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _Failing)
    tenant = uuid.uuid4()
    read_only = {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}
    async with app_session(tenant) as db:
        agent_id, run_id, _task = await _seed(db, tenant, frame=read_only)
    token = _token(tenant, agent_id, run_id)
    _code, body = await _rpc(
        token, "tools/call", {"name": "search_records", "arguments": {"model": "crm.lead"}}
    )
    assert body["result"]["isError"] is True
    _code, body = await _rpc(
        token, "tools/call", {"name": "create_record", "arguments": {"model": "sale.order"}}
    )
    assert body["result"]["isError"] is True
    await mcp_pool.close_all()

    denied = await _tool_events(app_session, tenant, "tool.call:create_record")
    assert [c.decision for c in denied] == ["deny"]
    (result,) = await _tool_events(app_session, tenant, "tool.result:")
    assert result.action == "tool.result:search_records"
    assert result.resource["outcome"] == "error"
    assert "SECRET-ROW-42" not in json.dumps(result.resource)
    assert result.reason is None


def _tenant_of(pg_url: str, table: str, row_id: object) -> uuid.UUID:
    """The tenant of a row a helper created, read past RLS."""
    import sqlalchemy as sa

    engine = sa.create_engine(pg_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.text(f"SELECT tenant_id FROM {table} WHERE id = :id"), {"id": str(row_id)}
            ).scalar_one()
    finally:
        engine.dispose()


async def test_a_held_rerun_audits_its_result_and_a_parked_one_does_not(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """The answered elicitation's re-run is a call of its own, in the engine and
    in the isolated control plane."""
    from tests.agent.test_engine_held_elicitation import _resume
    from tests.api.test_internal_agent import _resume_held_call

    # In-process engine: runs.
    result, _approvals, _ctx = await _resume(
        app_session, monkeypatch, answer='{"model": "sale.order"}', approved_original=True
    )
    assert result.status == "done"
    tenant = _tenant_of(pg_url, "task", result.task_id)
    (engine_result,) = await _tool_events(app_session, tenant, "tool.result:")
    assert engine_result.action == "tool.result:write_a"
    assert engine_result.resource["outcome"] == "ok"
    assert engine_result.resource["connection"] == "things"
    assert "run_id" in engine_result.resource

    # In-process engine: parked for an approval of the merged call.
    result, approvals, _ctx = await _resume(
        app_session, monkeypatch, answer="ACME", approval_actions=["modify"]
    )
    assert result.status == "waiting_for_approval"
    assert await _tool_events(app_session, approvals[0].tenant_id, "tool.result:") == []

    # Isolated control plane (/step): runs.
    reached, _body, ctx, _approvals = await _resume_held_call(
        app_session, monkeypatch, answer="ACME"
    )
    assert reached, "the held call ran"
    tenant = _tenant_of(pg_url, "mcp_connection", ctx["mcp_connection_id"])
    (isolated_result,) = await _tool_events(app_session, tenant, "tool.result:")
    assert isolated_result.action == "tool.result:create_record"
    assert isolated_result.resource["outcome"] == "ok"
    assert isolated_result.resource["connection"] == "odoo"
    assert "run_id" in isolated_result.resource


async def test_a_failing_result_audit_does_not_fail_the_call(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _broken(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr("oc8.audit.tool_calls.append_event", _broken)
    with caplog.at_level("WARNING", logger="oc8.audit.tool_calls"):
        tenant, _run_id, _s, status = await _run(app_session, monkeypatch, _one_read())
    assert status == "done"
    assert any("could not audit the result" in r.getMessage() for r in caplog.records)
    assert len(await _tool_events(app_session, tenant, "tool.call:read_a")) == 1


async def test_a_gateway_replay_audits_the_replayed_outcome(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replayed write is audited as ok, and a replayed error as an error."""
    from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token

    from oc8.agent import mcp_pool
    from oc8.agent.tool_idempotency import record_invocation

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    _FakeMcp.calls = []
    tenant = uuid.uuid4()
    failed = {"model": "sale.order", "qty": 1}
    async with app_session(tenant) as db:
        agent_id, run_id, task_id = await _seed(db, tenant)
        await record_invocation(
            db,
            tenant_id=tenant,
            task_id=task_id,
            tool="create_record",
            arguments=failed,
            result="ERROR: SECRET-ROW-42 rejected",
        )
    token = _token(tenant, agent_id, run_id)
    ok = {"model": "sale.order", "qty": 2}
    for arguments in (ok, ok, failed):
        await _rpc(token, "tools/call", {"name": "create_record", "arguments": arguments})
    await mcp_pool.close_all()

    assert len(_FakeMcp.calls) == 1, "the repeat and the recorded error were replayed"
    results = await _tool_events(app_session, tenant, "tool.result:create_record")
    assert [r.resource["outcome"] for r in results] == ["ok", "ok", "error"]
    assert "SECRET-ROW-42" not in json.dumps([r.resource for r in results])


async def _core_call_run(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    assistant: bool,
    with_connection: bool,
    chat_mode: str | None = None,
    call: ToolCall | None = None,
) -> tuple[uuid.UUID, uuid.UUID, Any]:
    """A chat run that calls the core tool todo_write -- through the Copilot
    (tenant Assistant) with no connection bound, like a live Copilot chat, or
    through an agent whose bound connection's frame refuses the call's right."""
    tenant = uuid.uuid4()
    recorder = _RecordingServer(delay_s=0.0)
    monkeypatch.setattr(
        "oc8.agent.mcp_client.McpSession",
        _session_factory(recorder, ["read_a"]),
    )
    call = call or ToolCall(
        id="c1",
        name="todo_write",
        arguments={"todos": [{"content": "check", "status": "completed"}]},
    )
    script = [_turn("", call), _turn("Done.")]
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", _ScriptedStream(script))
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": ["read_a"]})
        agent.is_tenant_assistant = assistant
        if with_connection:
            # todo_write is in none of the connection's scopes, so the frame
            # judges it as a modify -- which this frame does not grant.
            dept = await db.get(m.Department, agent.department_id)
            assert dept is not None
            dept.frame = {"tools": {"things": {"enabled": True, "read": True, "modify": False}}}
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
        db.add(session)
        await db.flush()
        context: dict[str, Any] = {"task": "go", "chat_session_id": str(session.id)}
        if chat_mode is not None:
            context["chat_mode"] = chat_mode
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, state="running", source="chat", context=context
        )
        db.add(run)
        await db.flush()
        result = await run_agent(
            db,
            agent=agent,
            task_text="go",
            tenant_id=tenant,
            mcp_conn=conn if with_connection else None,
            run_id=run.id,
        )
    return tenant, run.id, result


@pytest.mark.parametrize(
    ("assistant", "with_connection", "frame_code"),
    [
        (True, False, "not_this_connection"),
        (False, False, "not_this_connection"),
        (False, True, "right_not_granted"),
    ],
)
async def test_a_core_tool_that_ran_is_audited_as_allowed_with_its_result(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    assistant: bool,
    with_connection: bool,
    frame_code: str,
) -> None:
    """A reading core tool with no rule of its own runs under the frame's
    routine verdict on the run's bound connection (or on none at all). The
    audit records what applied -- it ran -- not that unrelated verdict (live
    2026-11-25: a Copilot chat's search_knowledge was audited as denied with
    "no tool connection bound to this run", and had no result event)."""
    tenant, run_id, result = await _core_call_run(
        app_session, monkeypatch, assistant=assistant, with_connection=with_connection
    )
    assert result.status == "done"
    assert result.todos == [{"content": "check", "status": "completed"}]
    (call,) = await _tool_events(app_session, tenant, "tool.call:todo_write")
    assert call.decision == "allow"
    # Not a policy ALLOW: marked, with the PEP's own verdict and its reason.
    assert call.reason is not None
    assert call.reason.startswith("core read run outside the connection frame; frame verdict: ")
    assert call.resource["basis"] == "core_read_outside_frame"
    assert call.resource["pdp"] == {"effect": "deny", "reason_code": frame_code}
    (event,) = await _tool_events(app_session, tenant, "tool.result:todo_write")
    assert event.decision == "allow"
    assert event.resource["run_id"] == str(run_id)
    assert event.resource["outcome"] == "ok"
    assert "connection" not in event.resource


async def test_a_core_tool_a_mode_withholds_stays_denied_and_does_not_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real refusal of a core tool is still audited as one, and stops it."""
    tenant, _run_id, result = await _core_call_run(
        app_session, monkeypatch, assistant=True, with_connection=False, chat_mode="ask"
    )
    assert result.status == "done"
    assert result.todos == []
    (call,) = await _tool_events(app_session, tenant, "tool.call:todo_write")
    assert call.decision == "deny"
    assert await _tool_events(app_session, tenant, "tool.result:") == []


async def test_the_isolated_runtime_audits_a_core_tool_that_ran_as_allowed(
    app_session: AppSessionFactory,
) -> None:
    """The isolated control plane records the same thing as the engine: a
    reading core tool that ran under the frame's routine verdict on the bound
    connection is an ALLOW with one result."""
    from tests.api.test_internal_agent import _mcp_backed_run, _post_tool

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # No modify on `odoo`: the frame judges todo_write (in no scope) a modify.
        agent_id, run_id = await _mcp_backed_run(db, tenant, modify=False)
    todos = [{"content": "check", "status": "completed"}]
    code, body = await _post_tool(tenant, agent_id, run_id, "todo_write", {"todos": todos})
    assert code == 200, body
    # It ran, and the SDK is told so (a "denied" status raises ToolDenied).
    assert body["status"] == "ok", body
    assert "Updated todo list" in body["output"], body
    (call,) = await _tool_events(app_session, tenant, "tool.call:todo_write")
    assert call.decision == "allow"
    (result,) = await _tool_events(app_session, tenant, "tool.result:todo_write")
    assert result.resource["outcome"] == "ok"
    assert "connection" not in result.resource


async def test_the_copilots_read_resource_without_a_grant_stays_denied(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_resource is decided on the connection it names: without a grant in
    the conversation the Copilot's read is refused, audited as such, and has
    no result -- although that refusal carries the routine denial's code."""
    tenant, _run_id, result = await _core_call_run(
        app_session,
        monkeypatch,
        assistant=True,
        with_connection=False,
        call=ToolCall(
            id="c1", name="read_resource", arguments={"connection": "things", "uri": "x://1"}
        ),
    )
    assert result.status == "done"
    (call,) = await _tool_events(app_session, tenant, "tool.call:read_resource")
    assert call.decision == "deny"
    assert "basis" not in call.resource
    assert await _tool_events(app_session, tenant, "tool.result:") == []


async def _isolated_core_run(
    app_session: AppSessionFactory,
    *,
    assistant: bool = True,
    with_task: bool = True,
    routes: dict[str, list[str]] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An isolated run with no connection bound (Copilot by default), or with
    `odoo` bound when `routes` route a name to it. Returns (tenant, agent, run)."""
    from tests.api.test_internal_agent import _mcp_backed_run, _plain_agent_run

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        if routes is None:
            agent, _task, run = await _plain_agent_run(db, tenant)
            agent_id, run_id = agent.id, run.id
        else:
            agent_id, run_id = await _mcp_backed_run(db, tenant)
            agent = await db.get(m.Agent, agent_id)
            run = await db.get(m.AgentRun, run_id)
            assert agent is not None and run is not None
            run.context = {**run.context, "tool_routes": routes}
        agent.is_tenant_assistant = assistant
        if not with_task:
            run.task_id = None
        await db.flush()
    return tenant, agent_id, run_id


_TODOS = {"todos": [{"content": "check", "status": "completed"}]}


async def test_the_isolated_runtime_audits_a_copilot_core_read_with_no_connection(
    app_session: AppSessionFactory,
) -> None:
    from tests.api.test_internal_agent import _post_tool

    tenant, agent_id, run_id = await _isolated_core_run(app_session)
    code, body = await _post_tool(tenant, agent_id, run_id, "todo_write", _TODOS)
    assert code == 200, body
    # It ran, and the SDK is told so (a "denied" status raises ToolDenied).
    assert body["status"] == "ok", body
    assert "Updated todo list" in body["output"], body
    (call,) = await _tool_events(app_session, tenant, "tool.call:todo_write")
    assert call.decision == "allow"
    assert call.resource["basis"] == "core_read_outside_frame"
    assert call.resource["pdp"] == {"effect": "deny", "reason_code": "not_this_connection"}
    assert "no tool connection bound to this run" in (call.reason or "")
    (result,) = await _tool_events(app_session, tenant, "tool.result:todo_write")
    assert result.resource["outcome"] == "ok"


async def test_the_isolated_runtime_never_audits_a_routed_namesake_as_a_core_read(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection's tool whose real name is a core tool's (`odoo.todo_write`
    -> `todo_write`) goes to the connection, never to the core tool. The
    Copilot has no grant for it: refused, audited as refused, not run."""
    from tests.api.test_internal_agent import _post_tool
    from tests.api.test_mcp_gateway import _FakeMcp

    from oc8.agent import mcp_pool

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    _FakeMcp.calls = []
    tenant, agent_id, run_id = await _isolated_core_run(
        app_session, routes={"odoo.todo_write": ["odoo", "todo_write"]}
    )
    code, body = await _post_tool(tenant, agent_id, run_id, "odoo.todo_write", _TODOS)
    await mcp_pool.close_all()
    assert code == 200, body
    assert body["status"] == "denied", body
    assert _FakeMcp.calls == []
    (call,) = await _tool_events(app_session, tenant, "tool.call:todo_write")
    assert call.decision == "deny"
    assert "basis" not in call.resource
    assert await _tool_events(app_session, tenant, "tool.result:") == []


async def test_the_isolated_runtime_audits_a_core_read_it_cannot_run_as_denied(
    app_session: AppSessionFactory,
) -> None:
    """No task to run it against: the core tool is not dispatched, so the
    routine denial is what applied."""
    from tests.api.test_internal_agent import _post_tool

    tenant, agent_id, run_id = await _isolated_core_run(app_session, with_task=False)
    code, body = await _post_tool(tenant, agent_id, run_id, "todo_write", _TODOS)
    assert code == 200, body
    assert body["status"] == "denied", body
    (call,) = await _tool_events(app_session, tenant, "tool.call:todo_write")
    assert call.decision == "deny"
    assert "basis" not in call.resource
    assert await _tool_events(app_session, tenant, "tool.result:") == []
