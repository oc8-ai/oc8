from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.constants import ACME_TENANT_ID
from oc8.runtime.adapter import EchoRuntimeStub, Oc8AgentRuntime
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_oc8_agent_runtime_delegates_to_run_agent(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    captured: dict[str, Any] = {}

    async def fake_run_agent(
        db: Any,
        *,
        agent: m.Agent,
        task_text: str,
        tenant_id: uuid.UUID,
        run_id: Any = None,
        mcp_conn: Any = None,
        parent_task_id: Any = None,
        delegation_depth: Any = 0,
        cancel_check: Any = None,
        inbox_check: Any = None,
        pre_decided: Any = None,
        originating_operator: Any = None,
        task_images_raw: Any = None,
        resume: Any = None,
    ) -> RunResult:
        captured["agent"] = agent
        captured["task_text"] = task_text
        captured["tenant_id"] = tenant_id
        captured["run_id"] = run_id
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=agent.id,
            status="done",
            output="ok",
            tool_calls=[],
            steps=1,
        )

    monkeypatch.setattr("oc8.runtime.adapter.run_agent", fake_run_agent)

    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        runtime = Oc8AgentRuntime()
        run_id = uuid.uuid4()
        result = await runtime.execute(
            db, agent=agent, task_text="do it", tenant_id=tenant, run_id=run_id
        )
        assert result.status == "done"
        assert captured["agent"] is agent
        assert captured["task_text"] == "do it"
        assert captured["tenant_id"] == tenant
        # Passed through so a resume leg can continue its suspended leg's task
        # rather than opening a second, orphaned one.
        assert captured["run_id"] == run_id


async def test_echo_runtime_stub_returns_canned_result(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        runtime = EchoRuntimeStub()
        result = await runtime.execute(db, agent=agent, task_text="hello", tenant_id=tenant)
        assert result.status == "done"
        assert "hello" in result.output
        assert result.agent_id == agent.id
        assert result.tool_calls == []
