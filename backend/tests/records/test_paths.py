"""Every tool path adds the same record links (spec §5 item 1).

The in-process engine, the MCP gateway of a containerized harness and the
isolated runtime's `/internal/agent/{run_id}/tool` each dispatch a tool call
on their own. One Odoo-like search runs through each of them here, against the
REAL odoo_mcp manifest, and all three must hand the model the same `oc8_url`
and register it as issued for the tenant.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import NeutralTool, ToolCall
from oc8.records.registry import issued_links
from tests.agent.test_engine_parallel_reads import _fixture, _ScriptedStream, _turn
from tests.api.test_internal_agent import _mcp_backed_run, _post_tool
from tests.api.test_mcp_gateway import _rpc, _seed, _token
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

#: tests/records/<this> -> tests -> backend -> repo root, where capas/ lives.
_CAPAS = Path(__file__).resolve().parents[3] / "capas"
URL = "https://odoo.example.com/odoo/sale.order/42"
OUTPUT = '[{"id": 42, "name": "S00042"}]'
ARGS = {"model": "sale.order"}
FOCUS: dict[str, Any] = {
    "entity_field": "model",
    "id_fields": ["id", "record_id"],
    "id_field": "id",
    "labels": {"sale.order": "Angebot"},
    "search_tools": ["search_records"],
}
ODOO_CONFIG: dict[str, Any] = {
    "focus_spec": FOCUS,
    "env": {"ODOO_URL": "https://odoo.example.com"},
    "_plugin_name": "odoo_mcp",
    "_connection_key": "primary",
}


@pytest.fixture(autouse=True)
def _real_odoo_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The record_url declaration is read off the plugin on disk; a test run
    starts in backend/, where the default `capas` root does not exist."""
    from oc8.capas import discovery

    real = discovery.find_plugin
    monkeypatch.setattr(
        discovery, "find_plugin", lambda plugin_id, paths=None: real(plugin_id, [str(_CAPAS)])
    )


async def _fake_pool_call(*_a: Any, tool: str, arguments: dict[str, Any], **_kw: Any) -> str:
    assert (tool, arguments) == ("search_records", ARGS)
    return OUTPUT


async def _issued(app_session: AppSessionFactory, tenant: uuid.UUID) -> set[str]:
    async with app_session(tenant) as db:
        return await issued_links(db, tenant_id=tenant, urls=[URL])


class _OdooSession:
    tools = [NeutralTool(name="search_records", description="", parameters={"type": "object"})]

    async def __aenter__(self) -> _OdooSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return await _fake_pool_call(tool=name, arguments=arguments)


class _SeeingStream(_ScriptedStream):
    """Keeps what the model was shown, turn by turn."""

    seen: list[str]

    def __call__(self, *args: Any, **kw: Any) -> Any:
        self.seen = [*getattr(self, "seen", []), repr(kw.get("messages"))]
        return super().__call__(*args, **kw)


async def test_the_engine_hands_the_model_a_record_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _open(*_a: Any, **_kw: Any) -> _OdooSession:
        return _OdooSession()

    stream = _SeeingStream(
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments=ARGS)),
            _turn("Done."),
        ]
    )
    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": ["search_records"]})
        conn.config = {**conn.config, **ODOO_CONFIG}
        await db.flush()
        await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert f'"oc8_url": "{URL}"' in stream.seen[-1]
    assert await _issued(app_session, tenant) == {URL}


async def test_the_gateway_hands_the_harness_a_record_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.mcp_pool.call", _fake_pool_call)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant, config_extra=ODOO_CONFIG)

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "search_records", "arguments": ARGS},
    )
    assert code == 200, body
    assert body["result"]["isError"] is False
    assert f'"oc8_url": "{URL}"' in body["result"]["content"][0]["text"]
    assert await _issued(app_session, tenant) == {URL}


async def test_the_isolated_runtime_hands_the_model_a_record_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.agent.mcp_pool.call", _fake_pool_call)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _mcp_backed_run(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        conn = await db.get(m.McpConnection, uuid.UUID(run.context["mcp_connection_id"]))
        assert conn is not None
        conn.config = {**conn.config, **ODOO_CONFIG}

    code, body = await _post_tool(tenant, agent_id, run_id, "search_records", ARGS)
    assert code == 200, body
    assert body["status"] == "ok"
    assert f'"oc8_url": "{URL}"' in body["output"]
    assert await _issued(app_session, tenant) == {URL}


async def test_a_failing_env_lookup_leaves_the_call_s_output_intact(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving the env for a link (a secret, an OAuth mint) can fail after
    the call itself succeeded. That costs the links, never the result."""

    async def _open(*_a: Any, **_kw: Any) -> _OdooSession:
        return _OdooSession()

    async def _no_env(*_a: Any, **_kw: Any) -> dict[str, str]:
        raise RuntimeError("secret store unreachable")

    stream = _SeeingStream(
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments=ARGS)),
            _turn("Done."),
        ]
    )
    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    monkeypatch.setattr("oc8.agent.engine.resolve_mcp_env", _no_env)
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": ["search_records"]})
        conn.config = {**conn.config, **ODOO_CONFIG}
        await db.flush()
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert result.status == "done"
    assert '[{"id": 42, "name": "S00042"}]' in stream.seen[-1]
    # The key as a JSON field; the prompt itself now names `oc8_url`.
    assert '"oc8_url":' not in stream.seen[-1]
    assert await _issued(app_session, tenant) == set()


async def test_the_engine_links_a_held_call_re_run_with_the_operator_s_answer(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that asked the operator a question finishes on the next leg,
    outside the step loop; its result is linked all the same."""
    created = "https://odoo.example.com/odoo/sale.order/43"

    class _Creating(_OdooSession):
        tools = [NeutralTool(name="create_record", description="", parameters={"type": "object"})]

        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            return '{"id": 43, "name": "S00043"}'

    stream = _SeeingStream([_turn("Done.")])
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", lambda *a, **kw: _Creating())
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(
            db, tenant, tool_scopes={"read": [], "modify": ["create_record"]}
        )
        conn.config = {**conn.config, **ODOO_CONFIG}
        held = {"model": "sale.order", "values": {}}
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            context={
                "pending_elicitation": {
                    "connection": "things",
                    "tool": "create_record",
                    "arguments": held,
                    "question": "Which customer?",
                },
                "clarifications": [{"question": "Which customer?", "answer": "ACME"}],
            },
        )
        db.add(run)
        await db.flush()
        await run_agent(
            db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn, run_id=run.id
        )

    assert "finished with the operator" in stream.seen[-1]
    assert f'"oc8_url": "{created}"' in stream.seen[-1]
    async with app_session(tenant) as db:
        assert await issued_links(db, tenant_id=tenant, urls=[created]) == {created}


async def test_a_gateway_replay_is_linked_without_touching_the_connection(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replayed write never reached the connection: linking it must not
    audit a `connection.used` nor leave a connect prompt on the run."""
    from sqlalchemy import select

    from oc8.agent.tool_idempotency import REPLAY_NOTE, record_invocation

    ran: list[str] = []

    async def _never(*_a: Any, tool: str, **_kw: Any) -> str:
        ran.append(tool)
        return "should not run"

    monkeypatch.setattr("oc8.agent.mcp_pool.call", _never)
    args = {"model": "sale.order", "values": {"name": "x"}}

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, task_id = await _seed(db, tenant, config_extra=ODOO_CONFIG)
        await record_invocation(
            db,
            tenant_id=tenant,
            task_id=task_id,
            tool="create_record",
            arguments=args,
            result='{"id": 43, "name": "S00043"}',
        )

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {"name": "create_record", "arguments": args},
    )
    assert code == 200, body
    text = body["result"]["content"][0]["text"]
    assert ran == []
    assert REPLAY_NOTE in text
    assert '"oc8_url": "https://odoo.example.com/odoo/sale.order/43"' in text
    async with app_session(tenant) as db:
        used = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "connection.used",
                    )
                )
            )
            .scalars()
            .all()
        )
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
    assert used == []
    assert "connection_required" not in run.context
