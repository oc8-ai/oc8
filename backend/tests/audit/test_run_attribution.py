from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from tests.conftest import AppSessionFactory
from tests.audit.helpers import flush_audit

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.constants import ACME_TENANT_ID
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result

pytestmark = pytest.mark.asyncio


class _OneToolThenStop:
    """First call asks for a tool (so run_agent writes ONE tool.call audit),
    second call stops (so the run terminates)."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        if self.calls == 1:
            return CompletionResult(
                text="", tool_calls=[ToolCall(id="c1", name="noop", arguments={})],
                usage=Usage(tokens_in=1, tokens_out=1), stop_reason="tool_use",
                provider="fake", model="fake",
            )
        return CompletionResult(
            text="done", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="fake", model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def test_run_agent_audit_attributes_to_originating_operator(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _OneToolThenStop())

    async with app_session(tenant) as s:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        s.add(dept)
        await s.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep")
        s.add(agent)
        await s.flush()
        await run_agent(
            s, agent=agent, task_text="do a thing", tenant_id=tenant,
            originating_operator="alice",
        )
        await flush_audit(s)  # the run's events reach the chain at commit
        ev = (await s.execute(
            select(m.AuditEvent).where(
                m.AuditEvent.category == "tool_action", m.AuditEvent.actor_id == agent.id
            )
        )).scalars().first()
        assert ev is not None, "run_agent should have written a tool.call audit event"
        # attributed to the triggering operator, NOT the agent
        assert ev.responsible_type == "operator" and ev.responsible_id == "alice"
