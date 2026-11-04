# backend/tests/coding/test_engine_classification.py
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

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
    """Only used as oc8.agent.engine.get_model_router. Captures the full
    request (not just messages, so contains_restricted can be asserted
    directly) it was called with, then completes with no tool calls."""

    def __init__(self) -> None:
        self.captured_messages: list[Any] = []
        self.captured_request: Any = None

    async def complete(self, req: Any) -> CompletionResult:
        self.captured_messages = list(req.messages)
        self.captured_request = req
        return CompletionResult(
            text="done", tool_calls=[], usage=Usage(tokens_in=3, tokens_out=3),
            stop_reason="stop", provider="fake", model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def _make_agent_with_grant(
    db: Any, tenant: uuid.UUID, *, frame: dict[str, Any]
) -> tuple[m.Agent, m.KnowledgeBase]:
    dept = m.Department(tenant_id=tenant, name="Legal", frame=frame)
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Counsel")
    db.add(agent)
    await db.flush()
    kb = m.KnowledgeBase(tenant_id=tenant, name="Contracts", embedding_model="nomic-embed-text")
    db.add(kb)
    await db.flush()
    db.add(
        m.KnowledgeGrant(tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id)
    )
    await db.flush()
    return agent, kb


async def _ingest_restricted(db: Any, tenant: uuid.UUID, kb: m.KnowledgeBase) -> None:
    await ingest_document(
        db, tenant_id=tenant, kb_id=kb.id, filename="nda.txt",
        content="the merger price is confidential and must not leak", content_type="text/plain",
    )
    chunks = (
        (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars().all()
    )
    for c in chunks:
        c.classification = "restricted"
    await db.flush()


async def test_restricted_chunk_excluded_by_default_clearance(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        agent, kb = await _make_agent_with_grant(db, tenant, frame={})
        await _ingest_restricted(db, tenant, kb)

        completion_router = _CapturingCompletionRouter()
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: completion_router)

        result = await run_agent(
            db,
            agent=agent,
            task_text="what should I know before signing the NDA?",
            tenant_id=tenant,
        )
        assert result.status == "done"
        joined = "\n".join(msg.content for msg in completion_router.captured_messages)
        assert "merger price" not in joined
        assert completion_router.captured_request.contains_restricted is False


async def test_restricted_chunk_included_when_cleared_and_local(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())

    frame = {"cleared_classes": ["public", "internal", "confidential", "restricted"]}
    async with app_session(tenant) as db:
        agent, kb = await _make_agent_with_grant(db, tenant, frame=frame)
        agent.presentation = {"provider": "ollama"}
        await _ingest_restricted(db, tenant, kb)

        completion_router = _CapturingCompletionRouter()
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: completion_router)

        result = await run_agent(
            db,
            agent=agent,
            task_text="what should I know before signing the NDA?",
            tenant_id=tenant,
        )
        assert result.status == "done"
        joined = "\n".join(msg.content for msg in completion_router.captured_messages)
        assert "merger price" in joined
        assert completion_router.captured_request.contains_restricted is True


async def test_restricted_chunk_excluded_for_cloud_even_when_cleared(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())

    frame = {"cleared_classes": ["public", "internal", "confidential", "restricted"]}
    async with app_session(tenant) as db:
        agent, kb = await _make_agent_with_grant(db, tenant, frame=frame)
        agent.presentation = {"provider": "anthropic"}
        await _ingest_restricted(db, tenant, kb)

        completion_router = _CapturingCompletionRouter()
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: completion_router)

        result = await run_agent(
            db,
            agent=agent,
            task_text="what should I know before signing the NDA?",
            tenant_id=tenant,
        )
        assert result.status == "done"
        joined = "\n".join(msg.content for msg in completion_router.captured_messages)
        assert "merger price" not in joined
        assert completion_router.captured_request.contains_restricted is False
