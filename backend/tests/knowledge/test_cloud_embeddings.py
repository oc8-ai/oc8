"""A knowledge base whose `embedding_model` is a cloud `<provider>/<model>`:
ingest and retrieval embed through the tenant's own provider credential, and
chunks stored without a vector can be re-embedded.

Real ModelRouter, real key resolution and the real OpenAI-compatible adapter;
only the HTTP call to the provider is faked.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.credentials.service import create_credential
from oc8.knowledge.chunks import live_chunks
from oc8.knowledge.ingest import ingest_document, reembed_knowledge_base
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

MODEL = "openai_compatible/nomic-embed-text"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _vector(text: str) -> list[float]:
    seed = sum(ord(c) for c in text) or 1
    return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _Provider:
    """The tenant's OpenAI-compatible embeddings endpoint. `down` makes every
    call a 503."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        self.down = False

        async def post(client: Any, url: str, json: Any = None, **kwargs: Any) -> Any:
            self.calls.append({"url": url, "json": json, "headers": kwargs.get("headers")})
            request = httpx.Request("POST", url, json=json)
            if self.down:
                return httpx.Response(503, json={"error": "down"}, request=request)
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": _vector(json["input"])}],
                    "usage": {"prompt_tokens": 5},
                },
                request=request,
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", post)


async def _setup(db: Any, tenant: uuid.UUID, *, model: str = MODEL) -> m.KnowledgeBase:
    await create_credential(
        db,
        tenant_id=tenant,
        name="Embeddings",
        credential_type="openai_compatible_api_key",
        field_values={"api_key": "sk-tenant", "base_url": "https://embed.example/v1"},
    )
    kb = m.KnowledgeBase(tenant_id=tenant, name="Handbook", embedding_model=model)
    db.add(kb)
    await db.flush()
    return kb


async def _vectors(db: Any, tenant: uuid.UUID, kb_id: uuid.UUID) -> list[list[float] | None]:
    stmt = live_chunks().where(m.KbChunk.tenant_id == tenant, m.KbChunk.kb_id == kb_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [None if r.embedding is None else list(r.embedding) for r in rows]


async def test_ingest_and_retrieval_embed_through_the_tenants_provider(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Provider(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _setup(db, tenant)
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="a.txt",
            content="the sky is blue",
            content_type="text/plain",
        )
        assert provider.calls, "ingest must embed through the provider"
        assert provider.calls[0]["url"] == "https://embed.example/v1/embeddings"
        assert provider.calls[0]["json"]["model"] == "nomic-embed-text"
        assert provider.calls[0]["headers"]["Authorization"] == "Bearer sk-tenant"
        vectors = await _vectors(db, tenant, kb.id)
        assert vectors and all(v is not None and len(v) == EMBED_DIM for v in vectors)

        # A cloud-model agent: a local-model agent's question stays local
        # (test_retrieval_policy.py), so it would not reach this provider.
        cfg = m.ModelConfig(
            tenant_id=tenant, provider="anthropic", model="claude", locality="cloud"
        )
        db.add(cfg)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=uuid.uuid4(), name="A", model_config_id=cfg.id
        )
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()
        before = len(provider.calls)
        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="sky")
        assert "the sky is blue" in ctx
        assert len(provider.calls) == before + 1  # the query was embedded too

        usage = (
            (
                await db.execute(
                    select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
                )
            )
            .scalars()
            .all()
        )
        assert len(usage) == len(provider.calls)
        assert {(u.provider, u.tokens_in) for u in usage} == {("openai_compatible", 5)}


async def test_retrieval_warns_with_the_reason_when_embedding_is_unavailable(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _Provider(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="KB", embedding_model="nosuch/embed")
        db.add(kb)
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()
        with caplog.at_level("WARNING", logger="oc8.knowledge.retrieval"):
            ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="q")
    assert ctx == ""
    [record] = [r for r in caplog.records if r.name == "oc8.knowledge.retrieval"]
    assert record.levelname == "WARNING"
    assert "nosuch" in record.getMessage() and "skipping" in record.getMessage()


async def test_reembed_fills_missing_vectors_and_is_idempotent(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Provider(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _setup(db, tenant)
        provider.down = True
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="a.txt",
            content="first paragraph " * 200,
            content_type="text/plain",
        )
        stored = await _vectors(db, tenant, kb.id)
        assert len(stored) > 1 and all(v is None for v in stored)

        # Still down: stops after the first failure, says why, changes nothing.
        calls = len(provider.calls)
        result = await reembed_knowledge_base(db, tenant_id=tenant, kb=kb)
        assert result["embedded"] == 0
        assert result["remaining"] == len(stored)
        assert "openai_compatible" in result["error"]
        assert len(provider.calls) == calls + 1

        provider.down = False
        result = await reembed_knowledge_base(db, tenant_id=tenant, kb=kb, limit=1)
        assert result == {"embedded": 1, "remaining": len(stored) - 1, "error": None}
        result = await reembed_knowledge_base(db, tenant_id=tenant, kb=kb)
        assert result == {"embedded": len(stored) - 1, "remaining": 0, "error": None}
        assert all(v is not None for v in await _vectors(db, tenant, kb.id))

        calls = len(provider.calls)
        result = await reembed_knowledge_base(db, tenant_id=tenant, kb=kb)
        assert result == {"embedded": 0, "remaining": 0, "error": None}
        assert len(provider.calls) == calls  # nothing left, nothing sent


async def test_reembed_is_tenant_scoped(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Provider(monkeypatch)
    tenant = uuid.uuid4()
    other = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _setup(db, tenant)
        provider.down = True
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="a.txt",
            content="x",
            content_type="text/plain",
        )
        kb_id = kb.id
    provider.down = False
    async with app_session(other) as db:
        foreign = m.KnowledgeBase(id=kb_id, tenant_id=tenant, name="x", embedding_model=MODEL)
        result = await reembed_knowledge_base(db, tenant_id=other, kb=foreign)
        assert result == {"embedded": 0, "remaining": 0, "error": None}
    async with app_session(tenant) as db:
        assert await _vectors(db, tenant, kb_id) == [None]
