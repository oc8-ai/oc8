"""Embeddings honour the same data policy as chat before content leaves for a
cloud provider.

Chat refuses to send `restricted` material to any non-local model
(`ModelRouter._guard_and_resolve`'s ClassificationViolation, and
`classification_rule` in retrieval), refuses a provider whose monthly limit is
used up or a tenant whose hard budget is exceeded, and honours strict BYOK. One
test per path where an embedding used to skip one of those. Real router, real
credentials, only HTTP is faked; every call is recorded so a test can assert
that NOTHING reached the cloud endpoint.
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
from oc8.memory.router import retrieve_context, write_memory
from oc8.metering.budget import set_budget
from oc8.metering.provider_budget import set_provider_budget
from oc8.modelrouter import EmbeddingUnavailable
from oc8.modelrouter.router import ModelRouter
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

CLOUD = "openai_compatible/nomic-embed-text"
CLOUD_URL = "https://embed.example/v1/embeddings"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


class _Http:
    """Answers both the cloud /embeddings endpoint and Ollama's /api/embeddings."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.urls: list[str] = []

        async def post(client: Any, url: str, json: Any = None, **kwargs: Any) -> Any:
            self.urls.append(url)
            request = httpx.Request("POST", url, json=json)
            vector = [0.5] * EMBED_DIM
            if url.endswith("/api/embeddings"):
                return httpx.Response(200, json={"embedding": vector}, request=request)
            return httpx.Response(
                200,
                json={"data": [{"embedding": vector}], "usage": {"prompt_tokens": 3}},
                request=request,
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", post)

    @property
    def cloud_calls(self) -> int:
        return sum(1 for u in self.urls if u == CLOUD_URL)

    @property
    def local_calls(self) -> int:
        return sum(1 for u in self.urls if u.endswith("/api/embeddings"))


async def _with_key(db: Any, tenant: uuid.UUID) -> None:
    await create_credential(
        db,
        tenant_id=tenant,
        name="Embeddings",
        credential_type="openai_compatible_api_key",
        field_values={"api_key": "sk-tenant", "base_url": "https://embed.example/v1"},
    )


async def _kb(db: Any, tenant: uuid.UUID, **kw: Any) -> m.KnowledgeBase:
    kb = m.KnowledgeBase(tenant_id=tenant, name="KB", embedding_model=CLOUD, **kw)
    db.add(kb)
    await db.flush()
    return kb


async def _chunks(db: Any, tenant: uuid.UUID, kb_id: uuid.UUID) -> list[m.KbChunk]:
    stmt = live_chunks().where(m.KbChunk.tenant_id == tenant, m.KbChunk.kb_id == kb_id)
    return list((await db.execute(stmt)).scalars().all())


async def _upload(db: Any, tenant: uuid.UUID, kb_id: uuid.UUID, text: str = "secret plan") -> None:
    await ingest_document(
        db,
        tenant_id=tenant,
        kb_id=kb_id,
        filename="a.txt",
        content=text,
        content_type="text/plain",
    )


# --------------------------------------------------- restricted classification


async def test_ingest_never_sends_a_restricted_bases_content_to_a_cloud_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        kb = await _kb(db, tenant, classification="restricted")
        await _upload(db, tenant, kb.id)
        chunks = await _chunks(db, tenant, kb.id)
    assert http.cloud_calls == 0
    assert chunks and all(c.embedding is None for c in chunks)


async def test_ingest_never_sends_a_restricted_sources_content_to_a_cloud_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.knowledge.connectors.base import RawDocument
    from oc8.knowledge.ingest import ingest_raw_document

    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        kb = await _kb(db, tenant)
        source = m.DataSource(
            tenant_id=tenant,
            connector_type="upload",
            name="hr",
            connected=True,
            classification="restricted",
        )
        db.add(source)
        await db.flush()
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=source,
            kb_id=kb.id,
            raw=RawDocument(
                source_uri="upload://hr.txt",
                title="hr.txt",
                content="salary table",
                content_type="text/plain",
                content_hash="h1",
                metadata={},
            ),
        )
        chunks = await _chunks(db, tenant, kb.id)
    assert http.cloud_calls == 0
    assert chunks and all(c.embedding is None for c in chunks)
    # And chat sees the label too: the chunk carries the source's classification,
    # so retrieval's classification_rule keeps it away from cloud chat models.
    assert all(c.classification == "restricted" for c in chunks)


async def test_ingested_chunks_carry_the_bases_classification(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: every chunk was stored as 'internal' whatever its base said, so
    retrieval handed a restricted base's text to a cloud chat model."""
    _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(
            tenant_id=tenant,
            name="KB",
            embedding_model="nomic-embed-text",
            classification="restricted",
        )
        db.add(kb)
        await db.flush()
        await _upload(db, tenant, kb.id)
        chunks = await _chunks(db, tenant, kb.id)
    assert chunks and all(c.classification == "restricted" for c in chunks)


async def test_reembed_never_sends_a_restricted_chunk_to_a_cloud_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        kb = await _kb(db, tenant)
        db.add(
            m.KbChunk(
                tenant_id=tenant,
                kb_id=kb.id,
                content="restricted text",
                source_uri="upload://x",
                chunk_metadata={},
                classification="restricted",
            )
        )
        await db.flush()
        result = await reembed_knowledge_base(db, tenant_id=tenant, kb=kb)
    assert http.cloud_calls == 0
    assert result["embedded"] == 0
    assert "restricted" in (result["error"] or "")


async def test_retrieval_does_not_embed_for_a_restricted_base_through_a_cloud_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        kb = await _kb(db, tenant, classification="restricted")
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()
        await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="q")
    assert http.cloud_calls == 0


# ------------------------------------------------------------ budgets / BYOK


async def test_embed_refuses_a_provider_whose_monthly_limit_is_used_up(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        await set_provider_budget(
            db, tenant_id=tenant, provider="openai_compatible", monthly_token_limit=0
        )
        with pytest.raises(EmbeddingUnavailable, match="monthly token limit"):
            await get_router().embed("x", model=CLOUD, tenant_id=tenant, db=db)
    assert http.cloud_calls == 0


async def test_embed_refuses_a_tenant_over_its_hard_budget(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        await set_budget(
            db, tenant_id=tenant, department_id=None, soft_limit_tokens=None, hard_limit_tokens=0
        )
        with pytest.raises(EmbeddingUnavailable, match="hard token budget"):
            await get_router().embed("x", model=CLOUD, tenant_id=tenant, db=db)
    assert http.cloud_calls == 0


async def test_strict_byok_refuses_the_platform_key(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = _Http(monkeypatch)
    from oc8.config import Settings

    router = ModelRouter(Settings(mistral_api_key="sk-platform", require_tenant_model_key=True))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(EmbeddingUnavailable, match="per-tenant API key"):
            await router.embed("x", model=CLOUD, tenant_id=tenant, db=db)
    assert http.cloud_calls == 0


def get_router() -> ModelRouter:
    from oc8.config import get_settings

    return ModelRouter(get_settings())


# ------------------------------------------------------------------- memory


async def _cloud_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "default_embedding_model", CLOUD)


async def _agent(db: Any, tenant: uuid.UUID, *, locality: str) -> m.Agent:
    cfg = m.ModelConfig(tenant_id=tenant, provider="ollama", model="llama3", locality=locality)
    if locality == "cloud":
        cfg.provider, cfg.model = "anthropic", "claude"
    db.add(cfg)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A", model_config_id=cfg.id)
    db.add(agent)
    await db.flush()
    return agent


async def test_memory_of_a_local_model_agent_never_goes_to_a_cloud_default(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local-model agent may hold restricted material (chat only lets
    restricted content reach local models), and what it writes to memory is
    derived from it."""
    http = _Http(monkeypatch)
    await _cloud_default(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        agent = await _agent(db, tenant, locality="local")
        await write_memory(db, tenant_id=tenant, agent=agent, tier="agent", content="note")
        await retrieve_context(db, agent=agent, tenant_id=tenant, frame={}, query_text="q")
    assert http.cloud_calls == 0


async def test_memory_falls_back_to_local_when_the_tenant_has_no_cloud_provider(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant that never set up the default's provider keeps local memory
    embeddings rather than none at all."""
    http = _Http(monkeypatch)
    await _cloud_default(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, locality="cloud")
        record = await write_memory(db, tenant_id=tenant, agent=agent, tier="agent", content="note")
    assert http.cloud_calls == 0
    assert http.local_calls == 1
    assert record.embedding is not None


async def test_memory_uses_the_cloud_default_when_the_tenant_has_it_and_policy_allows(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = _Http(monkeypatch)
    await _cloud_default(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        agent = await _agent(db, tenant, locality="cloud")
        await write_memory(db, tenant_id=tenant, agent=agent, tier="agent", content="note")
        rows = (
            (
                await db.execute(
                    select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
                )
            )
            .scalars()
            .all()
        )
    assert http.cloud_calls == 1
    assert len(rows) == 1


# --------------------------------------------------------- the default model


async def test_a_restricted_base_created_without_a_model_gets_a_local_one(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asgi_lifespan import LifespanManager

    from oc8.auth import get_identity_provider
    from oc8.main import create_app

    await _cloud_default(monkeypatch)
    tenant = uuid.uuid4()
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app()
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.post(
                "/api/v1/knowledge/bases",
                json={"name": "HR", "classification": "restricted"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["embeddingModel"] == "nomic-embed-text"

            r = await c.post(
                "/api/v1/knowledge/bases",
                json={"name": "HR2", "classification": "restricted", "embeddingModel": CLOUD},
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert "restricted" in r.text
