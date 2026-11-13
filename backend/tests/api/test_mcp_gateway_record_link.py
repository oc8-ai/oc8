# backend/tests/api/test_mcp_gateway_record_link.py
"""A held tool call carries a link to the record it is about (§6).

Built on test_mcp_gateway.py's own harness: the same `_seed`, the same minted
agent token, the same JSON-RPC shape. The connection's `focus_spec` is what
turns a call's arguments into (entity, ref) -- without it there is no record
identity and therefore no link, which is one of the cases asserted here.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.capas.manifest import RecordUrlTemplate, ToolPackConnection
from oc8.modelrouter import ToolCall
from tests.api.test_internal_agent import _EmptySchemaSession, _mcp_backed_run, _post_tool
from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

FOCUS: dict[str, Any] = {
    "entity_field": "model",
    "id_fields": ["id", "record_id"],
    "labels": {"sale.order": "Angebot"},
    "search_tools": ["search_records"],
}
TEMPLATE = RecordUrlTemplate(
    template="{base_url}/odoo/{model}/{id}",
    base_url_path=["env", "ODOO_URL"],
    models={"sale.order": "sale.order"},
)


def _declare(monkeypatch: pytest.MonkeyPatch, spec: RecordUrlTemplate | None) -> None:
    """Stand in for the pack on disk. The gateway resolves the template through
    `oc8.capas.discovery.resolve_tool_pack_connection`, and the test
    database's connection row is not backed by a real plugin folder."""
    conn = ToolPackConnection(key="primary", name="odoo", server_url="", record_url=spec)
    monkeypatch.setattr(
        "oc8.capas.discovery.resolve_tool_pack_connection", lambda plugin, key: conn
    )


async def _held_approval(app_session: AppSessionFactory, tenant: uuid.UUID) -> m.ApprovalRequest:
    async with app_session(tenant) as db:
        return (
            await db.execute(select(m.ApprovalRequest).where(m.ApprovalRequest.tenant_id == tenant))
        ).scalar_one()


async def test_a_held_write_gets_a_record_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_pool.McpSession", _FakeMcp)
    _declare(monkeypatch, TEMPLATE)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={
                "focus_spec": FOCUS,
                "env": {"ODOO_URL": "https://odoo.example.com"},
                "_plugin_name": "odoo_mcp",
                "_connection_key": "primary",
            },
        )

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {
            "name": "create_record",
            "arguments": {"model": "sale.order", "id": 42, "values": {"amount_total": 3900}},
        },
    )
    assert code == 200, body
    assert body["result"]["isError"] is True

    ar = await _held_approval(app_session, tenant)
    assert ar.action_type == "tool_send"
    assert ar.record_url == "https://odoo.example.com/odoo/sale.order/42"


async def test_a_connection_with_no_template_still_raises_the_approval(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degradation rule from §6, at the live call site: no template, no
    link, no error, and the run still parks exactly as before."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_pool.McpSession", _FakeMcp)
    _declare(monkeypatch, None)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={
                "focus_spec": FOCUS,
                "env": {"ODOO_URL": "https://odoo.example.com"},
                "_plugin_name": "odoo_mcp",
                "_connection_key": "primary",
            },
        )

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {
            "name": "create_record",
            "arguments": {"model": "sale.order", "id": 42, "values": {"amount_total": 3900}},
        },
    )
    assert code == 200, body
    assert body["result"]["isError"] is True
    assert _FakeMcp.calls == [], "the held action must still not run"

    ar = await _held_approval(app_session, tenant)
    assert ar.status == "pending"
    assert ar.record_url is None


async def test_a_call_that_names_no_single_record_gets_no_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `focus_spec` on the connection means `record_identity` has nothing to
    resolve -- the approval is raised with a null link rather than a guess."""
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_pool.McpSession", _FakeMcp)
    _declare(monkeypatch, TEMPLATE)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={
                "env": {"ODOO_URL": "https://odoo.example.com"},
                "_plugin_name": "odoo_mcp",
                "_connection_key": "primary",
            },
        )

    code, body = await _rpc(
        _token(tenant, agent_id, run_id),
        "tools/call",
        {
            "name": "create_record",
            "arguments": {"model": "sale.order", "values": {"amount_total": 3900}},
        },
    )
    assert code == 200, body
    ar = await _held_approval(app_session, tenant)
    assert ar.record_url is None


async def test_the_isolated_runtime_gets_the_same_record_link_on_a_held_write(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`internal_agent.py`'s own `/internal/agent/{run_id}/tool` funnels a
    held write through the identical `record_identity` + `link_for`
    wiring as the containerized gateway's
    `_call_tool` (Task 4's second call site) -- mirrors
    `test_internal_agent.py::test_resolved_b5_ask_does_not_park_again_on_internal_tool`'s
    destructive-tool-over-threshold setup, but for a FRESH call that has no
    pre-resolved operator verdict yet, so it actually parks."""
    _declare(monkeypatch, TEMPLATE)

    class _RecordingSession(_EmptySchemaSession):
        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            return "created"

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _RecordingSession)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id = await _mcp_backed_run(
            db,
            tenant,
            destructive_tools=["create_record"],
            approval_templates={"create_record": "Allow creating {id}?"},
            focus_spec=FOCUS,
            env={"ODOO_URL": "https://odoo.example.com"},
            create_tools=["create_record"],
        )

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "create_record",
        {"model": "sale.order", "id": 42, "values": {"amount_total": 3900}},
    )
    assert code == 200, body
    assert body["status"] == "waiting_for_approval"

    ar = await _held_approval(app_session, tenant)
    assert ar.record_url == "https://odoo.example.com/odoo/sale.order/42"


async def test_an_approved_create_links_its_customer_and_then_its_result(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §5 item 5, end to end on the gateway: the held create links the
    EXISTING customer it names, and once approved and run, the approval keeps
    the link of the record the call produced."""
    from oc8.agent.harness.calls import call_sig

    related = RecordUrlTemplate(
        template="{base_url}/odoo/{model}/{id}",
        base_url_path=["env", "ODOO_URL"],
        models={"sale.order": "sale.order", "res.partner": "res.partner"},
        related_fields={"values.partner_id": "res.partner"},
    )
    _declare(monkeypatch, related)
    ran: list[str] = []

    async def _created(*_a: Any, tool: str, **_kw: Any) -> str:
        ran.append(tool)
        return '{"id": 43, "name": "S00043"}'

    monkeypatch.setattr("oc8.agent.mcp_pool.call", _created)
    args = {"model": "sale.order", "values": {"partner_id": 7, "amount_total": 3900}}

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(
            db,
            tenant,
            config_extra={
                "focus_spec": FOCUS,
                "env": {"ODOO_URL": "https://odoo.example.com"},
                "_plugin_name": "odoo_mcp",
                "_connection_key": "primary",
            },
        )
    token = _token(tenant, agent_id, run_id)

    code, body = await _rpc(token, "tools/call", {"name": "create_record", "arguments": args})
    assert code == 200, body
    assert body["result"]["isError"] is True
    assert ran == []
    ar = await _held_approval(app_session, tenant)
    # No focus_spec label for res.partner here, so the entity names it.
    assert ar.related_links == [
        {"label": "res.partner #7", "url": "https://odoo.example.com/odoo/res.partner/7"}
    ]

    async with app_session(tenant) as db:
        approval = await db.get(m.ApprovalRequest, ar.id)
        run = await db.get(m.AgentRun, run_id)
        assert approval is not None and run is not None
        approval.status = "approved"
        run.state = "running"
        run.context = {
            **run.context,
            "resolved_tool_approvals": [
                {
                    "sig": call_sig(ToolCall(id="x", name="create_record", arguments=args)),
                    "tool": "create_record",
                    "arguments": args,
                    "decision": "approve",
                }
            ],
        }

    code, body = await _rpc(token, "tools/call", {"name": "create_record", "arguments": args})
    assert code == 200, body
    assert body["result"]["isError"] is False, body
    assert ran == ["create_record"]
    assert "https://odoo.example.com/odoo/sale.order/43" in body["result"]["content"][0]["text"]
    ar = await _held_approval(app_session, tenant)
    assert ar.result_url == "https://odoo.example.com/odoo/sale.order/43"
