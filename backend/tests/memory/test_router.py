# backend/tests/memory/test_router.py
from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.memory.router import (
    MAX_MEMORY_CONTENT_LENGTH,
    MemoryWriteError,
    resolve_memory_write_approval,
    retrieve_context,
    write_memory,
)
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    """Deterministic EMBED_DIM-dim fake embedding: identical text -> identical
    vector, so cosine similarity is reproducible without a real model."""

    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _FailingEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        from oc8.modelrouter import EmbeddingUnavailable

        raise EmbeddingUnavailable("ollama unreachable")


async def test_write_memory_agent_tier_is_approved_immediately(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        record = await write_memory(
            db, tenant_id=tenant, agent=agent, tier="agent", content="remember this"
        )
        assert record.status == "approved"
        assert record.embedding is not None
        assert len(record.embedding) == EMBED_DIM


async def test_write_memory_company_tier_is_pending(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        record = await write_memory(
            db, tenant_id=tenant, agent=agent, tier="company", content="company-wide fact"
        )
        assert record.status == "pending"


async def test_write_memory_rejects_empty_content(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        with pytest.raises(MemoryWriteError):
            await write_memory(db, tenant_id=tenant, agent=agent, tier="agent", content="   ")


async def test_write_memory_rejects_oversized_content(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        with pytest.raises(MemoryWriteError):
            await write_memory(
                db,
                tenant_id=tenant,
                agent=agent,
                tier="agent",
                content="x" * (MAX_MEMORY_CONTENT_LENGTH + 1),
            )


async def test_write_memory_embedding_unavailable_falls_back_to_null(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FailingEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        record = await write_memory(
            db, tenant_id=tenant, agent=agent, tier="agent", content="no embedder"
        )
        assert record.embedding is None
        assert record.status == "approved"


async def test_retrieve_context_returns_written_memory(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        await write_memory(
            db, tenant_id=tenant, agent=agent, tier="agent", content="the sky is blue"
        )
        ctx = await retrieve_context(
            db, agent=agent, tenant_id=tenant, frame={}, query_text="what color is the sky?"
        )
        assert "the sky is blue" in ctx
        assert "[Memory — Agent]" in ctx


async def test_retrieve_context_skips_rbac_denied_tier(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        # Write directly to the department store without going through the
        # (RBAC-gated) write_memory tier check, to prove retrieval itself
        # respects RBAC even if a record technically exists.
        store = m.MemoryStore(tenant_id=tenant, tier="department", owner_id=agent.department_id)
        db.add(store)
        await db.flush()
        db.add(
            m.MemoryRecord(
                tenant_id=tenant,
                store_id=store.id,
                content="dept secret",
                written_by=agent.id,
                status="approved",
            )
        )
        await db.flush()

        # No "memory" key in frame at all => department read denied.
        ctx = await retrieve_context(
            db, agent=agent, tenant_id=tenant, frame={}, query_text="secret?"
        )
        assert "dept secret" not in ctx


async def test_retrieve_context_excludes_pending_company_records(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        record = await write_memory(
            db, tenant_id=tenant, agent=agent, tier="company", content="not yet approved"
        )
        assert record.status == "pending"

        frame = {"memory": {"company": ["read"]}}
        ctx = await retrieve_context(
            db, agent=agent, tenant_id=tenant, frame=frame, query_text="approved?"
        )
        assert "not yet approved" not in ctx


async def test_resolve_memory_write_approval_approve_makes_record_visible(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        record = await write_memory(
            db, tenant_id=tenant, agent=agent, tier="company", content="approve me"
        )
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="memory_write",
            status="pending",
            payload={
                "memory_record_id": str(record.id),
                "tier": "company",
                "content": "approve me",
            },
        )
        db.add(ar)
        await db.flush()

        resolved = await resolve_memory_write_approval(db, approval_request=ar, decision="approve")
        assert resolved.status == "approved"

        frame = {"memory": {"company": ["read"]}}
        ctx = await retrieve_context(
            db, agent=agent, tenant_id=tenant, frame=frame, query_text="approve me"
        )
        assert "approve me" in ctx


async def test_resolve_memory_write_approval_reject_keeps_record_hidden(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        record = await write_memory(
            db, tenant_id=tenant, agent=agent, tier="company", content="reject me"
        )
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="memory_write",
            status="pending",
            payload={"memory_record_id": str(record.id), "tier": "company", "content": "reject me"},
        )
        db.add(ar)
        await db.flush()

        resolved = await resolve_memory_write_approval(db, approval_request=ar, decision="reject")
        assert resolved.status == "rejected"

        frame = {"memory": {"company": ["read"]}}
        ctx = await retrieve_context(
            db, agent=agent, tenant_id=tenant, frame=frame, query_text="reject me"
        )
        assert "reject me" not in ctx


async def test_retrieve_context_respects_token_budget(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.memory.router.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        # ~40 tokens each (160 chars / 4); budget of 50 tokens allows ~1 record.
        for i in range(5):
            await write_memory(
                db, tenant_id=tenant, agent=agent, tier="agent", content=("word " * 32) + str(i)
            )
        ctx = await retrieve_context(
            db,
            agent=agent,
            tenant_id=tenant,
            frame={},
            query_text="word",
            token_budget_per_tier=50,
        )
        # Budget-limited: not all 5 records fit.
        included = sum(ctx.count(("word " * 32) + str(i)) for i in range(5))
        assert 0 < included < 5
