from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.knowledge.worker import get_ingestion_queue, ingest_job
from oc8.main import create_app
from oc8.models.knowledge import EMBED_DIM
from oc8.runtime.worker import run_worker
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


async def _kb(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as s:
        kb = m.KnowledgeBase(
            tenant_id=tenant,
            name="KB",
            embedding_model="nomic-embed-text",
            classification="internal",
        )
        s.add(kb)
        await s.flush()
        return kb.id


async def test_create_source_returns_201_with_dto(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "website",
                    "name": "My Site",
                    "config": {"url": "https://example.com/"},
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["connectorType"] == "website"
            assert body["kind"] == "website"
            assert body["name"] == "My Site"
            assert body["connected"] is True


async def test_create_source_unknown_connector_type_400(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "does-not-exist",
                    "name": "Bad",
                    "config": {},
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 400


async def test_create_source_invalid_config_400(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "website",
                    "name": "Bad URL",
                    "config": {"url": "not-a-url"},
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant),
            )
            assert r.status_code == 400


async def test_preview_source_website(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.api.v1.knowledge as knowledge_api

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        return ("<p>hello preview world</p>", "text/html")

    monkeypatch.setattr(knowledge_api, "safe_fetch", fake_fetch)

    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            create_r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "website",
                    "name": "My Site",
                    "config": {"url": "https://example.com/"},
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant),
            )
            assert create_r.status_code == 201, create_r.text
            source_id = create_r.json()["id"]

            r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/preview",
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["items"]
            assert body["items"][0]["uri"] == "https://example.com/"
            assert "hello preview world" in body["sampleText"]


async def test_preview_source_non_text_sample_empty(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sample whose content-type is unsupported (e.g. JSON) must not 500.

    extract_text raises IngestionError for unsupported content-types; the
    preview should still return the discovered items with empty sampleText.
    """
    import oc8.api.v1.knowledge as knowledge_api

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        return ('{"hello": "world"}', "application/json")

    monkeypatch.setattr(knowledge_api, "safe_fetch", fake_fetch)

    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            create_r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "website",
                    "name": "JSON Site",
                    "config": {"url": "https://example.com/"},
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant),
            )
            assert create_r.status_code == 201, create_r.text
            source_id = create_r.json()["id"]

            r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/preview",
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["items"]
            assert body["sampleText"] == ""


async def test_preview_source_upload_empty_sample_text(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            create_r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "upload",
                    "name": "My Upload",
                    "config": {
                        "filename": "notes.txt",
                        "content": "hello",
                        "content_type": "text/plain",
                    },
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant),
            )
            assert create_r.status_code == 201, create_r.text
            source_id = create_r.json()["id"]

            r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/preview",
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["sampleText"] == ""


async def test_preview_source_not_found_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/knowledge/sources/{uuid.uuid4()}/preview",
                headers=_headers(tenant),
            )
            assert r.status_code == 404


async def test_sync_source_succeeds_and_writes_chunks(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.knowledge.connectors.website as w

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        return ("some page text " * 50, "text/html")

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)

    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            create_r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "website",
                    "name": "My Site",
                    "config": {"url": "https://example.com/", "maxPages": 1},
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant),
            )
            assert create_r.status_code == 201, create_r.text
            source_id = create_r.json()["id"]

            r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"kbId": str(kb_id)},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            # The endpoint enqueues and returns immediately -- it no longer runs
            # the sync inline, so the response is 'queued', not terminal.
            assert body["status"] == "queued"
            job_id = uuid.UUID(body["id"])

    # Drive the queued job to completion, same as the worker test does.
    await run_worker(get_ingestion_queue(), once=True, handler=ingest_job)

    async with app_session(tenant) as s:
        job = await s.get(m.IngestionJob, job_id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.stats["ingested"] == 1
        chunks = (
            (await s.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb_id))).scalars().all()
        )
        assert len(chunks) >= 1


async def test_sync_source_not_found_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    kb_id = await _kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/knowledge/sources/{uuid.uuid4()}/sync",
                json={"kbId": str(kb_id)},
                headers=_headers(tenant),
            )
            assert r.status_code == 404


async def test_preview_and_sync_source_cross_tenant_404(
    app_session: AppSessionFactory,
) -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    kb_id = await _kb(app_session, tenant_a)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            create_r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "website",
                    "name": "My Site",
                    "config": {"url": "https://example.com/"},
                    "kbId": str(kb_id),
                },
                headers=_headers(tenant_a),
            )
            assert create_r.status_code == 201, create_r.text
            source_id = create_r.json()["id"]

            preview_r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/preview",
                headers=_headers(tenant_b),
            )
            assert preview_r.status_code == 404

            sync_r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"kbId": str(kb_id)},
                headers=_headers(tenant_b),
            )
            assert sync_r.status_code == 404


async def test_sync_source_kb_cross_tenant_404(
    app_session: AppSessionFactory,
) -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    kb_id_a = await _kb(app_session, tenant_a)
    kb_id_b = await _kb(app_session, tenant_b)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            create_r = await client.post(
                "/api/v1/knowledge/sources",
                json={
                    "connectorType": "website",
                    "name": "My Site",
                    "config": {"url": "https://example.com/"},
                    "kbId": str(kb_id_a),
                },
                headers=_headers(tenant_a),
            )
            assert create_r.status_code == 201, create_r.text
            source_id = create_r.json()["id"]

            # kb_id belongs to another tenant
            r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"kbId": str(kb_id_b)},
                headers=_headers(tenant_a),
            )
            assert r.status_code == 404

            # kb_id doesn't exist at all
            r = await client.post(
                f"/api/v1/knowledge/sources/{source_id}/sync",
                json={"kbId": str(uuid.uuid4())},
                headers=_headers(tenant_a),
            )
            assert r.status_code == 404
