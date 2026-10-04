"""The in-process engine stores the same record link on a held write that the
containerized gateway and the isolated runtime do (§6) -- see
tests/api/test_mcp_gateway_record_link.py for the other two runtimes."""

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
from oc8.api.v1._serializers import approval_to_dto
from oc8.capas.manifest import RecordUrlTemplate, ToolPackConnection
from oc8.modelrouter import ToolCall

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


async def test_an_in_process_held_write_gets_a_record_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_conn = ToolPackConnection(
        key="primary", name="things", server_url="", record_url=TEMPLATE
    )
    monkeypatch.setattr(
        "oc8.approvals.record_url.resolve_tool_pack_connection",
        lambda plugin, key: manifest_conn,
    )
    recorder = _RecordingServer()

    async def _open(*_a: Any, **_kw: Any) -> _StubSession:
        return _StubSession(recorder, ["create_record"])

    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    monkeypatch.setattr(
        "oc8.agent.engine.stream_completion_with_fallback",
        _ScriptedStream(
            [
                _turn(
                    "",
                    ToolCall(
                        id="c1",
                        name="create_record",
                        arguments={"model": "sale.order", "id": 42, "values": {}},
                    ),
                ),
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
        conn.config = {
            **conn.config,
            "focus_spec": FOCUS,
            "env": {"ODOO_URL": "https://odoo.example.com"},
            "_plugin_name": "things_mcp",
            "_connection_key": "primary",
        }
        await db.flush()
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)
        approval = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.task_id == result.task_id)
            )
        ).scalar_one()

    assert result.status == "waiting_for_approval"
    assert recorder.windows == []
    assert approval.record_url == "https://odoo.example.com/odoo/sale.order/42"
    assert approval_to_dto(approval).record_url == "https://odoo.example.com/odoo/sale.order/42"
