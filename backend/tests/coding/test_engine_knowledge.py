# backend/tests/coding/test_engine_knowledge.py
from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.constants import ACME_TENANT_ID
from oc8.knowledge.ingest import ingest_document
from oc8.modelrouter import CompletionResult, Usage, chunk_from_result
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _CapturingCompletionRouter:
    """Only used as oc8.agent.engine.get_model_router, which never calls
    .embed() — that's oc8.knowledge.retrieval's (and oc8.memory.router's)
    own router, faked separately by _FakeEmbedRouter. Captures the messages
    it was called with, then completes with no tool calls so run_agent
    finishes immediately."""

    def __init__(self) -> None:
        self.captured_messages: list[Any] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.captured_messages = list(req.messages)
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(tokens_in=3, tokens_out=3),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _make_agent_with_kb_grant(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.KnowledgeBase]:
    dept = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep")
    db.add(agent)
    await db.flush()
    kb = m.KnowledgeBase(tenant_id=tenant, name="Playbook", embedding_model="nomic-embed-text")
    db.add(kb)
    await db.flush()
    db.add(
        m.KnowledgeGrant(tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id)
    )
    await db.flush()
    return agent, kb


async def test_run_agent_injects_kb_context_when_granted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        agent, kb = await _make_agent_with_kb_grant(db, tenant)
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="playbook.txt",
            content="always follow up within 24 hours",
            content_type="text/plain",
        )

        completion_router = _CapturingCompletionRouter()
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: completion_router)

        result = await run_agent(
            db, agent=agent, task_text="what's our follow-up policy?", tenant_id=tenant
        )
        assert result.status == "done"
        joined = "\n".join(msg.content for msg in completion_router.captured_messages)
        assert "always follow up within 24 hours" in joined
        assert "[Knowledge: Playbook]" in joined


async def test_run_agent_no_kb_context_without_grant(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Dev")
        db.add(agent)
        await db.flush()
        # No KB, no grant.

        completion_router = _CapturingCompletionRouter()
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: completion_router)

        result = await run_agent(db, agent=agent, task_text="anything", tenant_id=tenant)
        assert result.status == "done"
        joined = "\n".join(msg.content for msg in completion_router.captured_messages)
        assert "[Knowledge:" not in joined
