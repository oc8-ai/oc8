from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.knowledge.connectors.base import RawDocument
from oc8.knowledge.ingest import (
    extract_text,
    ingest_document,
    ingest_raw_document,
    run_source_sync,
)
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


async def _kb(s: object, tenant: uuid.UUID) -> m.KnowledgeBase:
    kb = m.KnowledgeBase(
        tenant_id=tenant, name="KB", embedding_model="nomic-embed-text", classification="internal"
    )
    s.add(kb)  # type: ignore[attr-defined]
    await s.flush()  # type: ignore[attr-defined]
    return kb


def test_extract_text_html_strips_tags() -> None:
    html = (
        "<html><head><style>p{color:red}</style></head>"
        "<body><p>Hello</p> <a>world</a></body></html>"
    )
    assert extract_text(content=html, content_type="text/html") == "Hello world"


def test_extract_text_html_drops_script() -> None:
    html = "<div>keep<script>var x = 1;</script></div>"
    assert extract_text(content=html, content_type="text/html") == "keep"


async def test_ingest_raw_document_writes_chunks(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        kb = await _kb(s, tenant)
        ds = m.DataSource(
            tenant_id=tenant, connector_type="website", name="src", config={}, connected=True
        )
        s.add(ds)
        await s.flush()
        raw = RawDocument(
            source_uri="https://x/",
            title="x",
            content="hello world " * 200,
            content_type="text/plain",
            content_hash="h1",
        )
        n = await ingest_raw_document(s, tenant_id=tenant, data_source=ds, kb_id=kb.id, raw=raw)
        assert n >= 1
        chunks = (
            (await s.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars().all()
        )
        assert len(chunks) == n
        assert all(c.source_uri == "https://x/" for c in chunks)
        assert all(c.embedding is not None for c in chunks)


async def test_ingest_raw_document_uses_the_knowledge_bases_own_embedding_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The knowledge base's `embedding_model` (chosen at creation, e.g.
    "local/bge-large") must reach the router -- not the deployment-wide
    default. Previously it never did: every ingest silently used
    Settings.default_embedding_model regardless of what a KB was created
    with."""
    seen_models: list[str | None] = []

    class _RecordingRouter:
        async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
            seen_models.append(model)
            seed = sum(ord(c) for c in text) or 1
            return [float((seed + i) % 23) for i in range(EMBED_DIM)]

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _RecordingRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        kb = m.KnowledgeBase(
            tenant_id=tenant,
            name="On-prem KB",
            embedding_model="local/bge-large",
            classification="restricted",
        )
        s.add(kb)
        await s.flush()
        ds = m.DataSource(
            tenant_id=tenant, connector_type="website", name="src", config={}, connected=True
        )
        s.add(ds)
        await s.flush()
        raw = RawDocument(
            source_uri="https://x/",
            title="x",
            content="hello world",
            content_type="text/plain",
            content_hash="h1",
        )
        await ingest_raw_document(s, tenant_id=tenant, data_source=ds, kb_id=kb.id, raw=raw)

    assert seen_models == ["local/bge-large"]


async def test_ingest_raw_document_html_content(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        kb = await _kb(s, tenant)
        ds = m.DataSource(
            tenant_id=tenant, connector_type="website", name="src", config={}, connected=True
        )
        s.add(ds)
        await s.flush()
        raw = RawDocument(
            source_uri="https://x/page",
            title="x",
            content="<p>" + ("word " * 400) + "</p>",
            content_type="text/html",
            content_hash="h2",
        )
        n = await ingest_raw_document(s, tenant_id=tenant, data_source=ds, kb_id=kb.id, raw=raw)
        assert n >= 1


async def test_run_source_sync_populates_stats_and_cursor(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.knowledge.connectors.website as w

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        return ("some page text " * 50, "text/html")

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        kb = await _kb(s, tenant)
        ds = m.DataSource(
            tenant_id=tenant,
            connector_type="website",
            name="src",
            config={"url": "https://ex.com/", "maxPages": 1},
            connected=True,
        )
        s.add(ds)
        await s.flush()
        job = await run_source_sync(s, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert job.status == "succeeded"
        assert job.stats.get("ingested") == 1
        assert job.stats.get("fetched") == 1
        assert job.stats.get("chunks", 0) >= 1
        assert ds.cursor.get("hashes")
        assert ds.doc_count >= 1
        assert ds.last_sync_status == "ok"
        assert ds.last_sync_error is None

        chunks = (
            (await s.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars().all()
        )
        assert len(chunks) == job.stats["chunks"]


async def test_run_source_sync_skips_cursor_hashes(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second sync over unchanged content ingests nothing (cursor skip)."""
    import oc8.knowledge.connectors.website as w

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        return ("stable content", "text/html")

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        kb = await _kb(s, tenant)
        ds = m.DataSource(
            tenant_id=tenant,
            connector_type="website",
            name="src",
            config={"url": "https://ex.com/", "maxPages": 1},
            connected=True,
        )
        s.add(ds)
        await s.flush()
        first = await run_source_sync(s, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert first.stats["ingested"] == 1
        second = await run_source_sync(s, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert second.status == "succeeded"
        assert second.stats["ingested"] == 0


async def test_run_source_sync_unknown_connector_marks_job_failed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector-resolution or fetch-loop failure must never leave the job
    stuck in "running" -- it must be marked "failed" with the error recorded."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        kb = await _kb(s, tenant)
        ds = m.DataSource(
            tenant_id=tenant,
            connector_type="does-not-exist",
            name="src",
            config={},
            connected=True,
        )
        s.add(ds)
        await s.flush()
        job = await run_source_sync(s, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert job.status == "failed"
        assert job.stats.get("error")
        # A2: a failed sync advances nothing. This assertion used to read
        # "last_sync_at is not None" -- bookkeeping ran outside the try, so an
        # expired token stamped the source as freshly synced. The job is the
        # record of what happened; the source is not touched.
        assert ds.last_sync_at is None
        # Unlike `last_sync_at`, this one MUST move on a failure -- it is the
        # whole point of the field: surface the failure on the source itself,
        # not only on a job row the UI has to already be polling to see.
        assert ds.last_sync_status == "failed"
        assert ds.last_sync_error == job.stats["error"]


async def test_ingest_document_creates_new_data_source_per_upload(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each upload must create its own DataSource (pre-refactor behavior),
    not reuse one by (tenant, connector_type="upload", filename) -- reuse
    collides source_uri and under-reports doc_count across uploads."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        kb = await _kb(s, tenant)
        job1 = await ingest_document(
            s,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="notes.txt",
            content="hello world",
            content_type="text/plain",
        )
        job2 = await ingest_document(
            s,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="notes.txt",
            content="hello world",
            content_type="text/plain",
        )
        assert job1.data_source_id != job2.data_source_id

        sources = (
            (
                await s.execute(
                    select(m.DataSource).where(m.DataSource.connector_type == "upload")
                )
            )
            .scalars()
            .all()
        )
        assert len(sources) == 2
        assert len({src.id for src in sources}) == 2

        chunks = (
            (await s.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars().all()
        )
        source_uris = {c.source_uri for c in chunks}
        assert len(source_uris) == 2
