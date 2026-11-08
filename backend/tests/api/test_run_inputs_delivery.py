"""A message written to a container run reaches it (spec §3.2): on the MCP
gateway's next tool result, and in the isolated control plane's transcript
before the next model call. Built on the gateway's and the control plane's own
test harnesses."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.main import create_app
from oc8.modelrouter.types import CompletionResult, Usage
from oc8.runtime.run_inputs import ConsumedInput
from tests.api.test_internal_agent import _agent_token, _as_stream, _plain_agent_run
from tests.api.test_mcp_gateway import _FakeMcp, _rpc, _seed, _token
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_TEXT = "Message from Alice during the run: use the EU address"


async def _queue(db: Any, tenant: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    alice = m.OrgMember(
        tenant_id=tenant, subject="alice", subject_uuid=uuid.uuid4(), display_name="Alice"
    )
    db.add(alice)
    await db.flush()
    row = m.RunInput(
        tenant_id=tenant, run_id=run_id, author_member_id=alice.id, text="use the EU address"
    )
    db.add(row)
    await db.flush()
    return row.id


def _record_publish(monkeypatch: pytest.MonkeyPatch) -> list[list[ConsumedInput]]:
    published: list[list[ConsumedInput]] = []

    async def _publish(
        tenant_id: uuid.UUID, *, run_id: uuid.UUID, consumed: list[ConsumedInput]
    ) -> None:
        if consumed:
            published.append(list(consumed))

    monkeypatch.setattr("oc8.runtime.run_inputs.publish_inputs_consumed", _publish)
    return published


async def _stamp(app_session: AppSessionFactory, tenant: uuid.UUID, input_id: uuid.UUID) -> Any:
    async with app_session(tenant) as db:
        return (await db.execute(select(m.RunInput).where(m.RunInput.id == input_id))).scalar_one()


async def test_the_next_tool_result_carries_the_message(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    monkeypatch.setattr("oc8.agent.mcp_pool.McpSession", _FakeMcp)
    published = _record_publish(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        input_id = await _queue(db, tenant, run_id)

    params = {"name": "search_records", "arguments": {"model": "crm.lead"}}
    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/call", params)
    assert code == 200, body
    texts = [item["text"] for item in body["result"]["content"]]
    assert "ok:search_records" in texts[0]
    assert texts[-1] == _TEXT
    assert [[c.id for c in batch] for batch in published] == [[input_id]]
    assert (await _stamp(app_session, tenant, input_id)).consumed_at is not None

    # Delivered once: the call after it carries nothing extra.
    code, body = await _rpc(_token(tenant, agent_id, run_id), "tools/call", params)
    assert code == 200, body
    assert all(_TEXT not in item["text"] for item in body["result"]["content"])


async def test_the_isolated_step_puts_the_message_in_the_transcript(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        sent.append(repr(args) + repr(kw))
        return CompletionResult(
            text="fertig",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    published = _record_publish(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant)
        input_id = await _queue(db, tenant, run.id)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run.id}/step",
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent.id, run.id)}"},
            )
            assert r.status_code == 200, r.text

    assert any(_TEXT in s for s in sent)
    assert [[c.id for c in batch] for batch in published] == [[input_id]]
    stamped = await _stamp(app_session, tenant, input_id)
    assert stamped.consumed_at is not None and stamped.consumed_step == 1
    async with app_session(tenant) as db:
        saved = await db.get(m.AgentRun, run.id)
        assert saved is not None
        transcript = saved.context["transcript"]
    assert any(entry.get("content") == _TEXT for entry in transcript)
