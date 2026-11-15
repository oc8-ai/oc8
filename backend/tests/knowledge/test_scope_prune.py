"""An admin narrowing a source's selection, and the sync that acts on it.

Narrowing is a deliberate act, so what it de-selects leaves on the next sync --
without reconcile's two-sync confirmation window, without its vanish-ratio hold,
and even when reconciliation is switched off. What it still needs is the one
fact reconcile also needs: an AUTHORITATIVE listing of what the narrowed source
now holds. Anything less keeps the flag and prunes nothing, because "I could not
see it" is not "the admin took it out".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import Settings, get_settings
from oc8.knowledge.connectors import registry
from oc8.knowledge.connectors.base import (
    Attestation,
    RawDocument,
    SourceListing,
    ValidationResult,
)
from oc8.knowledge.ingest import run_source_sync
from oc8.knowledge.scope_prune import prune_out_of_scope
from oc8.knowledge.tombstone import SCOPE_CHANGED
from oc8.main import create_app
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

STUB = "test_scope_prune"
T0 = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "selection": {"type": "array", "x-oc8-picker": True},
    },
}


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _Connector:
    type_id = STUB
    requires_oauth: str | None = None
    label = "Stub"
    description = "test connector"
    config_schema: dict[str, Any] = _SCHEMA

    def __init__(
        self,
        *,
        docs: list[RawDocument] | None = None,
        listing: SourceListing | None = None,
        fail: bool = False,
        attest_raises: bool = False,
        valid: bool = True,
    ) -> None:
        self.docs = docs or []
        self.listing = listing
        self.fail = fail
        self.attest_raises = attest_raises
        self.valid = valid
        self.attested = 0

    async def validate(self, config: dict[str, Any], auth: Any = None) -> ValidationResult:
        if self.valid:
            return ValidationResult(ok=True)
        return ValidationResult(ok=False, error="that selection does not exist")

    async def discover(self, config: dict[str, Any], auth: Any = None) -> list[Any]:
        return []

    async def fetch(self, config: dict[str, Any], cursor: Any, auth: Any = None) -> Any:
        if self.fail:
            raise RuntimeError("the remote went away")
        for doc in self.docs:
            yield doc

    async def attest_listing(self, config: dict[str, Any], auth: Any = None) -> SourceListing:
        self.attested += 1
        self.attested_config = dict(config)
        if self.attest_raises:
            raise RuntimeError("could not list")
        assert self.listing is not None
        return self.listing


def _authoritative(*uris: str) -> SourceListing:
    return SourceListing(attestation=Attestation.AUTHORITATIVE, present_uris=frozenset(uris))


def _raw(uri: str, content: str) -> RawDocument:
    return RawDocument(
        source_uri=uri,
        title=uri,
        content=content,
        content_type="text/plain",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )


@pytest.fixture
def reconcile_off(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = get_settings()
    monkeypatch.setattr(settings, "knowledge_reconcile_enabled", False, raising=False)
    return settings


@pytest.fixture
def reconcile_on(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = get_settings()
    monkeypatch.setattr(settings, "knowledge_reconcile_enabled", True, raising=False)
    return settings


@pytest.fixture(autouse=True)
def _embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())


async def _kb(db: Any, tenant: uuid.UUID, name: str = "KB") -> m.KnowledgeBase:
    kb = m.KnowledgeBase(tenant_id=tenant, name=name, embedding_model="nomic-embed-text")
    db.add(kb)
    await db.flush()
    return kb


async def _source(
    db: Any, tenant: uuid.UUID, *, cursor: dict[str, Any] | None = None
) -> m.DataSource:
    ds = m.DataSource(
        tenant_id=tenant,
        connector_type=STUB,
        name="src",
        config={"selection": [1, 2, 3]},
        cursor=cursor or {},
        connected=True,
    )
    db.add(ds)
    await db.flush()
    return ds


async def _document(
    db: Any, tenant: uuid.UUID, *, kb_id: uuid.UUID, ds_id: uuid.UUID, uri: str
) -> None:
    db.add(
        m.KbChunk(
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds_id,
            content="body",
            embedding=None,
            source_uri=uri,
            chunk_metadata={"chunk_index": 0},
        )
    )
    await db.flush()


async def _chunks(db: Any, kb_id: uuid.UUID) -> dict[str, m.KbChunk]:
    rows = (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb_id))).scalars().all()
    return {c.source_uri: c for c in rows}


async def _flag(db: Any, ds: m.DataSource) -> bool:
    await db.refresh(ds, ["cursor"])
    return bool((ds.cursor or {}).get("scope_changed"))


# ------------------------------------------------------------------ PATCH


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_patch_config_runs_validate(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit drawer can no longer save a config the connector would refuse
    at creation."""
    monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector(valid=False))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        ds = await _source(db, tenant)
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.patch(
            f"/api/v1/knowledge/sources/{source_id}",
            json={"name": "renamed", "config": {"selection": [9]}},
            headers=_headers(tenant),
        )
        assert r.status_code == 400, r.text
        assert "that selection does not exist" in r.text

    async with app_session(tenant) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.config == {"selection": [1, 2, 3]}
        assert reloaded.name == "src"
        assert not (reloaded.cursor or {}).get("scope_changed")


async def test_patch_picker_change_flags_scope(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        flagged = await _source(db, tenant, cursor={"hashes": ["h"]})
        plain = await _source(db, tenant)
        flagged_id, plain_id = flagged.id, plain.id

    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.patch(
            f"/api/v1/knowledge/sources/{flagged_id}",
            json={"config": {"selection": [1, 2]}},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text
        # A non-picker config key and a rename are not a scope change, and
        # neither is resending the picker unchanged.
        r = await c.patch(
            f"/api/v1/knowledge/sources/{plain_id}",
            json={"name": "renamed", "config": {"name": "x", "selection": [1, 2, 3]}},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        flagged = await db.get(m.DataSource, flagged_id)
        plain = await db.get(m.DataSource, plain_id)
        assert flagged is not None and plain is not None
        assert flagged.cursor["scope_changed"] is True
        assert flagged.cursor["hashes"] == ["h"], "the rest of the cursor is kept"
        assert flagged.config == {"selection": [1, 2]}
        assert "scope_changed" not in (plain.cursor or {})
        assert plain.config == {"selection": [1, 2, 3], "name": "x"}


# ------------------------------------------------------------------ sync


async def test_scope_prune_removes_deselected_documents(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    tenant = uuid.uuid4()
    docs = [_raw(f"test://{n}", f"document {n}") for n in ("a", "b", "c")]
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector(docs=docs))
        first = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert first.stats["ingested"] == 3
        assert "scope_pruned" not in first.stats

        ds.cursor = {**ds.cursor, "scope_changed": True}
        await db.flush()
        # The hash cursor makes an unchanged document never re-yielded, which is
        # exactly why the listing, not the fetch, decides what is still in scope.
        monkeypatch.setitem(
            registry._CONNECTORS,
            STUB,
            _Connector(docs=docs[:2], listing=_authoritative("test://a", "test://b")),
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.stats["scope_pruned"] == 1
        chunks = await _chunks(db, kb.id)
        assert chunks["test://c"].deleted_at is not None
        assert chunks["test://c"].deleted_reason == SCOPE_CHANGED
        assert chunks["test://c"].reduced_at is None, "an inference keeps its grace window"
        assert chunks["test://a"].deleted_at is None
        assert chunks["test://b"].deleted_at is None
        assert not await _flag(db, ds)
        # Forgotten by the hash cursor, so re-selecting it brings it back.
        assert "test://c" not in ds.cursor["uri_hashes"]
        assert docs[2].content_hash not in ds.cursor["hashes"]
        assert ds.doc_count == 2


async def test_scope_prune_ignores_vanish_hold(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        for n in "abcd":
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=f"test://{n}")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://a"))
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.stats["scope_pruned"] == 3
        chunks = await _chunks(db, kb.id)
        assert {u for u, c in chunks.items() if c.deleted_at is None} == {"test://a"}
        assert not await _flag(db, ds)


async def test_scope_prune_runs_before_reconcile_and_does_not_trip_its_hold(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_on: Settings
) -> None:
    """Prune first, then observe. Pruned documents leave the source's live
    baseline, so reconcile neither counts them toward its vanish ratio (which
    would have held the source) nor marks them missing."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        for n in "abcdefghij":
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=f"test://{n}")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://a"))
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.stats["scope_pruned"] == 9
        assert job.stats["reconcile"] == "observed"
        await db.refresh(ds, ["reconcile_state"])
        assert ds.reconcile_state == "ok"
        chunks = await _chunks(db, kb.id)
        assert all(c.missing_since is None for c in chunks.values())


async def test_scope_prune_covers_every_linked_base(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """The attestation is about the source, so the prune is too: a source
    feeding two bases loses the de-selected document from both, whichever base
    this sync was queued for. Another source's copy of the URI is untouched."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb1 = await _kb(db, tenant, "one")
        kb2 = await _kb(db, tenant, "two")
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        other = await _source(db, tenant)
        for kb in (kb1, kb2):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://keep")
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://drop")
        await _document(db, tenant, kb_id=kb2.id, ds_id=other.id, uri="test://drop")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://keep"))
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb1.id)

        assert job.stats["scope_pruned"] == 2
        for kb in (kb1, kb2):
            rows = (
                (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id)))
                .scalars()
                .all()
            )
            for c in rows:
                gone = c.source_uri == "test://drop" and c.data_source_id == ds.id
                assert (c.deleted_at is not None) is gone, (kb.name, c.source_uri)


async def test_prune_out_of_scope_only_touches_the_named_bases(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb1 = await _kb(db, tenant, "one")
        kb2 = await _kb(db, tenant, "two")
        ds = await _source(db, tenant)
        for kb in (kb1, kb2):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://drop")
        pruned = await prune_out_of_scope(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_ids=[kb1.id],
            present_uris=frozenset(),
            now=T0,
        )
        assert pruned == 1
        assert (await _chunks(db, kb1.id))["test://drop"].deleted_at == T0
        assert (await _chunks(db, kb2.id))["test://drop"].deleted_at is None


async def test_scope_prune_waits_for_authoritative_listing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        monkeypatch.setitem(
            registry._CONNECTORS,
            STUB,
            _Connector(listing=SourceListing(attestation=Attestation.NONE, reason="paged")),
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert "scope_pruned" not in job.stats
        assert (await _chunks(db, kb.id))["test://a"].deleted_at is None
        assert await _flag(db, ds)


async def test_scope_prune_waits_out_removals_and_a_raising_attest(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        for connector in (
            _Connector(
                listing=SourceListing(
                    attestation=Attestation.REMOVALS, removed_uris=frozenset({"test://a"})
                )
            ),
            _Connector(attest_raises=True),
        ):
            monkeypatch.setitem(registry._CONNECTORS, STUB, connector)
            job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
            assert connector.attested >= 1
            assert "scope_pruned" not in job.stats
            assert (await _chunks(db, kb.id))["test://a"].deleted_at is None
            assert await _flag(db, ds)


async def test_scope_prune_refuses_an_empty_listing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """An expired credential answering AUTHORITATIVE with nothing is the wire
    shape of "everything was de-selected". A narrowing always leaves something
    selected, so an empty listing prunes nothing and keeps the flag."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector(listing=_authoritative()))
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert "scope_pruned" not in job.stats
        assert (await _chunks(db, kb.id))["test://a"].deleted_at is None
        assert await _flag(db, ds)


async def test_scope_prune_skipped_on_fatal_sync(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        connector = _Connector(fail=True, listing=_authoritative("test://b"))
        monkeypatch.setitem(registry._CONNECTORS, STUB, connector)
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.status == "failed"
        assert connector.attested == 0
        assert "scope_pruned" not in job.stats
        assert (await _chunks(db, kb.id))["test://a"].deleted_at is None
        assert await _flag(db, ds)


async def test_scope_prune_attests_the_selection_the_edit_saved(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """An edit committed while the sync was fetching: the in-memory config
    predates it, and attesting that old selection would clear the flag without
    pruning what the edit took out."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://b")
        # Written behind the ORM's back, as a concurrent PATCH would be.
        await db.execute(
            update(m.DataSource)
            .where(m.DataSource.id == ds.id)
            .values(config={"selection": [1]}, cursor={"scope_changed": True})
            .execution_options(synchronize_session=False)
        )
        assert ds.config == {"selection": [1, 2, 3]}
        connector = _Connector(listing=_authoritative("test://a"))
        monkeypatch.setitem(registry._CONNECTORS, STUB, connector)
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert connector.attested_config == {"selection": [1]}
        assert job.stats["scope_pruned"] == 1
