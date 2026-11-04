"""PATCH /knowledge/bases/{id} and the paged search/filter/group list on GET
/knowledge/bases (Design System Consistency plan, Task 5).

Same documented exception as Task 4 (test_datasource_patch_and_list_query.py):
DELETE /knowledge/bases/{id} stays untouched and out of scope here -- it is the
documented, irreversible operator-delete path (`tombstone_base`), and there is
no restore-the-row endpoint for a KnowledgeBase, on purpose.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.knowledge.chunks import live_chunks
from oc8.main import create_app
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _base(
    db: object,
    tenant: uuid.UUID,
    *,
    name: str = "base",
    description: str = "",
    classification: str = "internal",
    deleted: bool = False,
) -> m.KnowledgeBase:
    kb = m.KnowledgeBase(
        tenant_id=tenant,
        name=name,
        description=description,
        embedding_model="nomic-embed-text",
        status="current",
        classification=classification,
    )
    if deleted:
        import datetime as dt

        kb.deleted_at = dt.datetime.now(tz=dt.UTC)
    db.add(kb)
    await db.flush()
    return kb


# ------------------------------------------------------------------- PATCH


async def test_patch_base_updates_name_and_description(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _base(db, tenant, name="Old Base")
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/bases/{kb_id}",
                json={"name": "Renamed Base", "description": "new"},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "Renamed Base"
            assert body["description"] == "new"

    async with app_session(tenant) as db:
        reloaded = await db.get(m.KnowledgeBase, kb_id)
        assert reloaded is not None
        assert reloaded.name == "Renamed Base"
        assert reloaded.description == "new"


async def test_patch_base_partial_update_leaves_other_fields_untouched(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _base(db, tenant, name="Keep Name", description="keep desc")
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/bases/{kb_id}",
                json={"description": "changed"},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "Keep Name"
            assert body["description"] == "changed"

    async with app_session(tenant) as db:
        reloaded = await db.get(m.KnowledgeBase, kb_id)
        assert reloaded is not None
        assert reloaded.name == "Keep Name"
        assert reloaded.description == "changed"


async def test_patch_base_changes_embedding_model_before_any_content(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _base(db, tenant, name="Empty Base")
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/bases/{kb_id}",
                json={"embeddingModel": "local/other-model"},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            assert r.json()["embeddingModel"] == "local/other-model"

    async with app_session(tenant) as db:
        reloaded = await db.get(m.KnowledgeBase, kb_id)
        assert reloaded is not None
        assert reloaded.embedding_model == "local/other-model"


async def test_patch_base_refuses_embedding_model_change_on_a_populated_external_index(
    app_session: AppSessionFactory,
) -> None:
    """An external index's vectors live outside oc8 and cannot be re-embedded
    from here, so its model stays locked once it holds content."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _base(db, tenant, name="Populated Base")
        kb.index_type = "qdrant"
        kb.freshness = {"docs": 1, "chunks": 5}
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/bases/{kb_id}",
                json={"embeddingModel": "local/other-model"},
                headers=_headers(tenant),
            )
            assert r.status_code == 409, r.text

    async with app_session(tenant) as db:
        reloaded = await db.get(m.KnowledgeBase, kb_id)
        assert reloaded is not None
        assert reloaded.embedding_model == "nomic-embed-text"


class _EmbedAs:
    """Every text embeds to `value` repeated; remembers the models asked for."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.models: list[str | None] = []
        self.tenants: list[object] = []

    async def embed(self, text: str, model: str | None = None, **kw: object) -> list[float]:
        self.models.append(model)
        self.tenants.append(kw.get("tenant_id"))
        return [self.value] * EMBED_DIM


async def _populated(db: object, tenant: uuid.UUID, *, embedding: list[float] | None) -> uuid.UUID:
    kb = await _base(db, tenant, name="Populated Base")
    kb.freshness = {"docs": 1, "chunks": 2}
    for i in range(2):
        db.add(
            m.KbChunk(
                tenant_id=tenant,
                kb_id=kb.id,
                content=f"chunk {i}",
                embedding=embedding,
                source_uri="upload://a.txt",
                chunk_metadata={"chunk_index": i},
            )
        )
    await db.flush()
    return kb.id


async def _embeddings(db: object, kb_id: uuid.UUID) -> list[list[float] | None]:
    rows = (await db.execute(live_chunks().where(m.KbChunk.kb_id == kb_id))).scalars().all()
    return [None if r.embedding is None else list(r.embedding) for r in rows]


async def test_patch_base_changing_the_model_re_embeds_existing_chunks(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _EmbedAs(2.0)
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: fake)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb_id = await _populated(db, tenant, embedding=[1.0] * EMBED_DIM)

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/bases/{kb_id}",
                json={"embeddingModel": "acme/embed-768"},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text
            assert r.json()["embeddingModel"] == "acme/embed-768"

    assert fake.models == ["acme/embed-768", "acme/embed-768"]
    assert fake.tenants == [tenant, tenant]
    async with app_session(tenant) as db:
        assert await _embeddings(db, kb_id) == [[2.0] * EMBED_DIM] * 2


async def test_reembed_endpoint_fills_missing_vectors(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _EmbedAs(3.0)
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: fake)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb_id = await _populated(db, tenant, embedding=None)

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(f"/api/v1/knowledge/bases/{kb_id}/reembed", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            assert r.json() == {"embedded": 2, "remaining": 0, "error": None}
            # Idempotent: nothing left to do the second time.
            r = await c.post(f"/api/v1/knowledge/bases/{kb_id}/reembed", headers=_headers(tenant))
            assert r.json() == {"embedded": 0, "remaining": 0, "error": None}
            # Another tenant cannot see the base at all.
            r = await c.post(
                f"/api/v1/knowledge/bases/{kb_id}/reembed", headers=_headers(uuid.uuid4())
            )
            assert r.status_code == 404

    async with app_session(tenant) as db:
        assert await _embeddings(db, kb_id) == [[3.0] * EMBED_DIM] * 2


async def test_embedding_models_lists_the_deployment_default_first(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "default_embedding_model", "acme_ai/embed-768")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/embedding-models", headers=_headers(uuid.uuid4()))
            assert r.status_code == 200, r.text
            assert r.json() == {
                "defaultModel": "acme_ai/embed-768",
                "models": ["acme_ai/embed-768", "local/nomic-embed-text"],
            }


async def test_create_base_without_a_model_uses_the_deployment_default(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "default_embedding_model", "acme/embed-768")
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.post(
                "/api/v1/knowledge/bases", json={"name": "KB"}, headers=_headers(uuid.uuid4())
            )
            assert r.status_code == 201, r.text
            assert r.json()["embeddingModel"] == "acme/embed-768"


async def test_patch_base_not_found_404(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/bases/{uuid.uuid4()}",
                json={"name": "x"},
                headers=_headers(tenant),
            )
            assert r.status_code == 404


async def test_patch_base_cross_tenant_404(app_session: AppSessionFactory) -> None:
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    async with app_session(owner) as db:
        kb = await _base(db, owner)
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.patch(
                f"/api/v1/knowledge/bases/{kb_id}",
                json={"name": "hijacked"},
                headers=_headers(stranger),
            )
            assert r.status_code == 404

    async with app_session(owner) as db:
        reloaded = await db.get(m.KnowledgeBase, kb_id)
        assert reloaded is not None
        assert reloaded.name != "hijacked"


async def test_patch_base_requires_knowledge_manage(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _base(db, tenant)
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            for role in ("auditor", "operator", "dept_manager"):
                r = await c.patch(
                    f"/api/v1/knowledge/bases/{kb_id}",
                    json={"name": "nope"},
                    headers=_headers(tenant, role),
                )
                assert r.status_code == 403, role


# --------------------------------------------------------------- list-query


async def test_list_bases_returns_paged_envelope_and_excludes_deleted(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        live = await _base(db, tenant, name="Live Base")
        gone = await _base(db, tenant, name="Gone Base", deleted=True)
        live_id, gone_id = live.id, gone.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/bases", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert "totalCount" in body
            assert "items" in body
            ids = [b["id"] for b in body["items"]]
            assert str(live_id) in ids
            assert str(gone_id) not in ids
            assert body["totalCount"] == len(body["items"])


async def test_list_bases_include_archived_shows_deleted(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        gone = await _base(db, tenant, name="Gone Base", deleted=True)
        gone_id = gone.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get(
                "/api/v1/knowledge/bases?includeArchived=true", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            ids = [b["id"] for b in r.json()["items"]]
            assert str(gone_id) in ids


async def test_list_bases_search_matches_name(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _base(db, tenant, name="Acme Handbook")
        await _base(db, tenant, name="Other Notes")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/bases?search=acme", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            names = [b["name"] for b in r.json()["items"]]
            assert names == ["Acme Handbook"]


async def test_list_bases_group_by_sensitivity(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _base(db, tenant, name="B", classification="internal")
        await _base(db, tenant, name="A", classification="confidential")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get(
                "/api/v1/knowledge/bases?group_by=sensitivity", headers=_headers(tenant)
            )
            assert r.status_code == 200, r.text
            sensitivities = [b["sensitivity"] for b in r.json()["items"]]
            assert sensitivities == sorted(sensitivities)


async def test_list_bases_pagination(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        for i in range(5):
            await _base(db, tenant, name=f"K{i}")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/bases?limit=2&offset=1", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["items"]) == 2
            assert body["totalCount"] == 5


async def test_list_bases_cross_tenant_isolation(app_session: AppSessionFactory) -> None:
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    async with app_session(owner) as db:
        await _base(db, owner, name="Owner Base")

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.get("/api/v1/knowledge/bases", headers=_headers(stranger))
            assert r.status_code == 200, r.text
            assert r.json()["items"] == []
            assert r.json()["totalCount"] == 0
