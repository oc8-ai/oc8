from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.constants import ACME_TENANT_ID
from oc8.memory.router import retrieve_context
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _MemoryWriteScriptedRouter:
    """Emits one memory_write tool call for the given tier, then a final
    text with no tool calls. Only used as oc8.agent.engine.get_model_router,
    which never calls .embed() — that's oc8.memory.router's own router,
    faked separately by _FakeEmbedRouter."""

    def __init__(self, tier: str) -> None:
        self._tier = tier
        self._calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self._calls += 1
        if self._calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="memory_write",
                        arguments={"tier": self._tier, "content": "the client prefers email"},
                    )
                ],
                usage=Usage(tokens_in=5, tokens_out=5),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )
        return CompletionResult(
            text="noted",
            tool_calls=[],
            usage=Usage(tokens_in=3, tokens_out=3),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _make_agent(db: Any, tenant: uuid.UUID) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name="Sales", frame={"memory": {"company": ["read"]}})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep")
    db.add(agent)
    await db.flush()
    return agent


async def test_memory_write_agent_tier_completes_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(
        "oc8.agent.engine.get_model_router", lambda: _MemoryWriteScriptedRouter("agent")
    )
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        result = await run_agent(db, agent=agent, task_text="remember this", tenant_id=tenant)
        assert result.status == "done"
        assert any(
            str(tc.get("result", "")).startswith("memory recorded")
            for tc in result.tool_calls
            if "result" in tc
        )


async def test_memory_write_company_tier_suspends_for_approval(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(
        "oc8.agent.engine.get_model_router", lambda: _MemoryWriteScriptedRouter("company")
    )
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        result = await run_agent(db, agent=agent, task_text="remember this", tenant_id=tenant)
        assert result.status == "waiting_for_approval"

        from sqlalchemy import select

        ar = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "memory_write")
            )
        ).scalar_one()
        assert ar.status == "pending"
        assert ar.payload["tier"] == "company"
        record = await db.get(m.MemoryRecord, uuid.UUID(ar.payload["memory_record_id"]))
        assert record is not None and record.status == "pending"


async def test_prior_agent_memory_is_retrieved_in_a_later_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)

        # Run 1: agent writes a memory.
        monkeypatch.setattr(
            "oc8.agent.engine.get_model_router", lambda: _MemoryWriteScriptedRouter("agent")
        )
        await run_agent(db, agent=agent, task_text="remember this", tenant_id=tenant)

        # Run 2: verify retrieve_context (what run_agent injects as context)
        # surfaces the memory written in run 1.
        ctx = await retrieve_context(
            db,
            agent=agent,
            tenant_id=tenant,
            frame={},
            query_text="what do we know about the client?",
        )
        assert "the client prefers email" in ctx
