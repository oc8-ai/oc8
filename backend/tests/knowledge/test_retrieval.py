from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.knowledge.ingest import ingest_document
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.modelrouter import EmbeddingUnavailable
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _FailingEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        raise EmbeddingUnavailable("ollama unreachable")


async def test_retrieve_kb_context_returns_content_when_agent_granted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(
            tenant_id=tenant, name="Handbook", embedding_model="nomic-embed-text"
        )
        db.add(kb)
        await db.flush()
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="a.txt",
            content="the sky is blue",
            content_type="text/plain",
        )
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

        ctx, _ = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="sky color"
        )
        assert "the sky is blue" in ctx
        assert "[Knowledge: Handbook]" in ctx
        assert "source: upload://" in ctx


async def test_retrieve_kb_context_empty_without_grant(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Secret", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="a.txt",
            content="classified info",
            content_type="text/plain",
        )
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        # No grant created for this agent.

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="info")
        assert ctx == ""


async def test_retrieve_kb_context_via_department_grant(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        dept_id = uuid.uuid4()
        kb = m.KnowledgeBase(
            tenant_id=tenant, name="Dept Docs", embedding_model="nomic-embed-text"
        )
        db.add(kb)
        await db.flush()
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="a.txt",
            content="department policy details",
            content_type="text/plain",
        )
        agent = m.Agent(tenant_id=tenant, department_id=dept_id, name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="department", grantee_id=dept_id
            )
        )
        await db.flush()

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="policy")
        assert "department policy details" in ctx


async def test_retrieve_kb_context_respects_token_budget(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Big", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

        # ~150 chars/~38 tokens each, single chunk per doc (under CHUNK_SIZE);
        # budget of 50 tokens fits exactly one.
        for i in range(5):
            await ingest_document(
                db,
                tenant_id=tenant,
                kb_id=kb.id,
                filename=f"doc{i}.txt",
                content=("word " * 30) + str(i),
                content_type="text/plain",
            )

        ctx, _ = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="word", token_budget=50
        )
        included = sum(ctx.count(("word " * 30) + str(i)) for i in range(5))
        assert 0 < included < 5


async def test_retrieve_kb_context_fails_open_on_embed_unavailable(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FailingEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        kb = m.KnowledgeBase(tenant_id=tenant, name="X", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="anything")
        assert ctx == ""


async def test_retrieve_kb_context_excludes_restricted_for_cloud_locality(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Legal", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        await ingest_document(
            db, tenant_id=tenant, kb_id=kb.id, filename="a.txt",
            content="top secret merger terms", content_type="text/plain",
        )
        chunk = (
            await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))
        ).scalar_one()
        chunk.classification = "restricted"
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

        frame = {"cleared_classes": ["public", "internal", "confidential", "restricted"]}
        ctx, contains_restricted = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="merger terms",
            frame=frame, model_locality="cloud",
        )
        assert ctx == ""
        assert contains_restricted is False


async def test_retrieve_kb_context_includes_restricted_for_local_locality(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Legal", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        await ingest_document(
            db, tenant_id=tenant, kb_id=kb.id, filename="a.txt",
            content="top secret merger terms", content_type="text/plain",
        )
        chunk = (
            await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))
        ).scalar_one()
        chunk.classification = "restricted"
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

        frame = {"cleared_classes": ["public", "internal", "confidential", "restricted"]}
        ctx, contains_restricted = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="merger terms",
            frame=frame, model_locality="local",
        )
        assert "top secret merger terms" in ctx
        assert contains_restricted is True


async def test_retrieve_kb_context_excludes_uncleared_classification_regardless_of_locality(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="HR", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        await ingest_document(
            db, tenant_id=tenant, kb_id=kb.id, filename="a.txt",
            content="confidential salary bands", content_type="text/plain",
        )
        chunk = (
            await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))
        ).scalar_one()
        chunk.classification = "confidential"
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

        frame = {"cleared_classes": ["public"]}  # confidential not cleared
        ctx, contains_restricted = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="salary",
            frame=frame, model_locality="local",
        )
        assert ctx == ""
        assert contains_restricted is False
