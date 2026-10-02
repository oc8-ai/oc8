"""External vector-index retrieval: granted remote KBs surface in search_knowledge."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from oc8 import models as m
from oc8.capas import contributions, loader
from oc8.capas.discovery import find_plugin
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.config import get_settings
from oc8.constants import ACME_TENANT_ID
from oc8.knowledge.ingest import ingest_document
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.knowledge.vector_indexes.base import VectorIndexError
from oc8.knowledge.vector_indexes.registry import resolve_vector_index
from oc8.modelrouter import EmbeddingUnavailable
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).resolve().parents[1] / "plugins" / "fixtures"


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None) -> list[float]:
        seed = sum(ord(c) for c in (text + (model or ""))) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(FIXTURES))
    get_settings.cache_clear()
    loader.reset_for_tests()
    contributions.reset_for_tests()
    yield
    loader.reset_for_tests()
    contributions.reset_for_tests()
    get_settings.cache_clear()


async def _enable_demo_index(session: AppSessionFactory, tenant: uuid.UUID) -> None:
    found = find_plugin("demo_vector_index")
    assert found is not None and found.manifest is not None
    async with session(tenant) as db:
        from oc8.capas.service import DuplicateVersionError

        try:
            version = await install_plugin(db, tenant_id=tenant, manifest_data=found.manifest)
            capa_id = version.capa_id
            permissions = list(version.permissions)
        except DuplicateVersionError:
            from sqlalchemy import select

            from oc8.models import Capa, CapaVersion

            capa = (
                await db.execute(
                    select(Capa).where(Capa.tenant_id == tenant, Capa.name == "demo_vector_index")
                )
            ).scalar_one()
            capa_id = capa.id
            version_row = (
                await db.execute(select(CapaVersion).where(CapaVersion.capa_id == capa.id))
            ).scalar_one()
            permissions = list(version_row.permissions)
        await enable_plugin(
            db,
            tenant_id=tenant,
            capa_id=capa_id,
            granted_permissions=permissions,
        )


async def test_external_kb_appears_in_retrieve_when_granted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    await _enable_demo_index(app_session, tenant)
    async with app_session(tenant) as db:
        # Demo index does not read the credential at search time; the id is
        # still required on the KB row for the external-index contract.
        kb = m.KnowledgeBase(
            tenant_id=tenant,
            name="Remote Handbook",
            embedding_model="nomic-embed-text",
            index_type="demo_index",
            index_config={"collection": "hr"},
            credential_id=uuid.uuid4(),
        )
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

        ctx, _ = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="vacation policy"
        )
        assert "remote hit for vacation policy in hr" in ctx
        assert "[Knowledge: Remote Handbook]" in ctx
        assert "demo-index://hr/1" in ctx


async def test_external_kb_empty_without_grant(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    await _enable_demo_index(app_session, tenant)
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(
            tenant_id=tenant,
            name="Secret Remote",
            embedding_model="nomic-embed-text",
            index_type="demo_index",
            index_config={"collection": "x"},
            credential_id=uuid.uuid4(),
        )
        db.add(kb)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="x")
        assert ctx == ""


async def test_internal_and_external_merge(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    await _enable_demo_index(app_session, tenant)
    async with app_session(tenant) as db:
        internal = m.KnowledgeBase(
            tenant_id=tenant, name="Local", embedding_model="nomic-embed-text"
        )
        external = m.KnowledgeBase(
            tenant_id=tenant,
            name="Remote",
            embedding_model="nomic-embed-text",
            index_type="demo_index",
            index_config={"collection": "ext"},
            credential_id=uuid.uuid4(),
        )
        db.add_all([internal, external])
        await db.flush()
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=internal.id,
            filename="a.txt",
            content="the sky is blue",
            content_type="text/plain",
        )
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        for kb in (internal, external):
            db.add(
                m.KnowledgeGrant(
                    tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
                )
            )
        await db.flush()

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="sky")
        assert "the sky is blue" in ctx
        assert "remote hit for sky in ext" in ctx


async def test_tenant_without_capa_cannot_resolve_index(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(VectorIndexError):
            await resolve_vector_index(db, tenant_id=tenant, type_id="demo_index")


async def test_ingest_rejects_external_kb(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.knowledge.ingest import IngestionError

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    await _enable_demo_index(app_session, tenant)
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(
            tenant_id=tenant,
            name="Remote",
            embedding_model="nomic-embed-text",
            index_type="demo_index",
            index_config={"collection": "hr"},
            credential_id=uuid.uuid4(),
        )
        db.add(kb)
        await db.flush()
        with pytest.raises(IngestionError, match="external vector index"):
            await ingest_document(
                db,
                tenant_id=tenant,
                kb_id=kb.id,
                filename="a.txt",
                content="nope",
                content_type="text/plain",
            )


async def test_embed_failure_skips_kb_not_whole_retrieve(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SelectiveEmbed:
        async def embed(self, text: str, model: str | None = None) -> list[float]:
            if model and "fail" in model:
                raise EmbeddingUnavailable("boom")
            seed = sum(ord(c) for c in text) or 1
            return [float((seed + i) % 23) for i in range(EMBED_DIM)]

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _SelectiveEmbed())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    await _enable_demo_index(app_session, tenant)
    async with app_session(tenant) as db:
        good = m.KnowledgeBase(
            tenant_id=tenant, name="Good", embedding_model="nomic-embed-text"
        )
        bad = m.KnowledgeBase(
            tenant_id=tenant,
            name="Bad",
            embedding_model="fail-model",
            index_type="demo_index",
            index_config={"collection": "x"},
            credential_id=uuid.uuid4(),
        )
        db.add_all([good, bad])
        await db.flush()
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=good.id,
            filename="a.txt",
            content="kept content",
            content_type="text/plain",
        )
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        for kb in (good, bad):
            db.add(
                m.KnowledgeGrant(
                    tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
                )
            )
        await db.flush()

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text="kept")
        assert "kept content" in ctx
        assert "remote hit" not in ctx
