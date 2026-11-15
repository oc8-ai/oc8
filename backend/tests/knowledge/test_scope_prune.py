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
from oc8.knowledge.reconcile import sweep_knowledge_deletions
from oc8.knowledge.scope_prune import SCOPE_CHANGED_EVENT, empty_pickers, prune_out_of_scope
from oc8.knowledge.tombstone import (
    DOCUMENT_DELETED,
    DOCUMENT_REDUCED,
    SCOPE_CHANGED,
    restore_document,
)
from oc8.main import create_app
from oc8.models.knowledge import EMBED_DIM
from tests.audit.helpers import flush_audit
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


ADMIN = "0b9c1d2e-3f40-4a5b-8c6d-7e8f9a0b1c2d"


def _headers(tenant: uuid.UUID, subject: str = "op") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
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
        # The flag carries who narrowed the scope, so the prune can name them.
        assert flagged.cursor["scope_changed"] == {"by": "op"}
        assert flagged.cursor["hashes"] == ["h"], "the rest of the cursor is kept"
        assert flagged.config == {"selection": [1, 2]}
        assert "scope_changed" not in (plain.cursor or {})
        assert plain.config == {"selection": [1, 2, 3], "name": "x"}


async def test_patch_refuses_an_empty_picker(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that selects nothing is a source to delete, not to narrow. The
    check is generic, off the schema marker, and refuses before anything is
    applied -- the connector's own validate() says yes here."""
    monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        ds = await _source(db, tenant)
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        for config in (
            {"selection": []},
            {"selection": None},
            {"selection": ""},
            {"selection": {}},
        ):
            r = await c.patch(
                f"/api/v1/knowledge/sources/{source_id}",
                json={"name": "renamed", "config": config},
                headers=_headers(tenant),
            )
            assert r.status_code == 400, r.text
            assert "selection" in r.text

    async with app_session(tenant) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.config == {"selection": [1, 2, 3]}
        assert reloaded.name == "src"
        assert not (reloaded.cursor or {}).get("scope_changed")


async def test_patch_records_who_changed_the_scope(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        ds = await _source(db, tenant)
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.patch(
            f"/api/v1/knowledge/sources/{source_id}",
            json={"config": {"selection": [1, 2], "name": "not a picker"}},
            headers=_headers(tenant, ADMIN),
        )
        assert r.status_code == 200, r.text
        # An edit that leaves the pickers alone records no scope change.
        r = await c.patch(
            f"/api/v1/knowledge/sources/{source_id}",
            json={"config": {"name": "still not a picker"}},
            headers=_headers(tenant, ADMIN),
        )
        assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.cursor["scope_changed"] == {"by": ADMIN}
        await flush_audit(db)
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == SCOPE_CHANGED_EVENT,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        ev = events[0]
        assert (ev.responsible_type, ev.responsible_id) == ("operator", ADMIN)
        assert str(ev.actor_id) == ADMIN
        assert ev.resource["data_source_id"] == str(source_id)
        # Only the picker properties, before and after.
        assert ev.resource["scope"] == {"selection": {"before": [1, 2, 3], "after": [1, 2]}}


async def test_create_refuses_an_empty_picker(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        for config in ({}, {"selection": []}, {"selection": ""}):
            r = await c.post(
                "/api/v1/knowledge/sources",
                json={"connectorType": STUB, "name": "s", "config": config, "kbId": str(kb_id)},
                headers=_headers(tenant),
            )
            assert r.status_code == 400, r.text
            assert "selection" in r.text
        r = await c.post(
            "/api/v1/knowledge/sources",
            json={
                "connectorType": STUB,
                "name": "s",
                "config": {"selection": [1]},
                "kbId": str(kb_id),
            },
            headers=_headers(tenant),
        )
        assert r.status_code == 201, r.text


def test_empty_pickers_treats_every_falsy_value_as_empty() -> None:
    for config in (
        {},
        {"selection": None},
        {"selection": []},
        {"selection": ""},
        {"selection": {}},
    ):
        assert empty_pickers(_SCHEMA, config) == ["selection"], config
    for config in ({"selection": [0]}, {"selection": "x"}, {"selection": {"a": 1}}):
        assert empty_pickers(_SCHEMA, config) == [], config
    # Only marked properties count.
    assert empty_pickers(_SCHEMA, {"selection": [1], "name": ""}) == []


async def test_patch_keeps_a_cursor_written_since_it_loaded_the_row(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is merged into the cursor as it is NOW, not as it was when the
    request loaded the row: a sync committing its digests in between must not be
    rolled back by an edit to the picker."""
    tenant = uuid.uuid4()

    class _SyncLandsDuringValidate(_Connector):
        async def validate(self, config: dict[str, Any], auth: Any = None) -> ValidationResult:
            async with app_session(tenant) as other:
                ds = await other.get(m.DataSource, source_id)
                assert ds is not None
                ds.cursor = {"hashes": ["written-by-a-sync"]}
                await other.flush()
            return ValidationResult(ok=True)

    monkeypatch.setitem(registry._CONNECTORS, STUB, _SyncLandsDuringValidate())
    async with app_session(tenant) as db:
        ds = await _source(db, tenant, cursor={"hashes": ["old"]})
        source_id = ds.id

    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.patch(
            f"/api/v1/knowledge/sources/{source_id}",
            json={"config": {"selection": [1]}},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        reloaded = await db.get(m.DataSource, source_id)
        assert reloaded is not None
        assert reloaded.cursor == {"hashes": ["written-by-a-sync"], "scope_changed": {"by": "op"}}


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
        # A deliberate act: the de-selected text leaves oc8 now, not after a
        # grace window that only the (optional) reconcile sweep would close.
        assert chunks["test://c"].reduced_at is not None
        assert chunks["test://c"].content == ""
        assert chunks["test://c"].embedding is None
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


async def test_an_empty_listing_prunes_everything_but_recoverably(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """PATCH refuses an empty picker, and the attestation contract makes a
    connector RAISE on a listing error -- but "everything is gone" is still the
    one answer an expired credential could fake. So the documents leave
    retrieval at once and keep their text for the grace window, restorable,
    and the source says so in its notes."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://b")
        monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector(listing=_authoritative()))
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.stats["scope_pruned"] == 2
        assert "scope_emptied" in job.stats["notes"]
        chunks = await _chunks(db, kb.id)
        assert all(c.deleted_at is not None for c in chunks.values())
        assert all(c.deleted_reason == SCOPE_CHANGED for c in chunks.values())
        assert all(c.reduced_at is None and c.content == "body" for c in chunks.values())
        assert not await _flag(db, ds)

        restored = await restore_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://a",
            now=T0,
        )
        assert restored == 1
        chunks = await _chunks(db, kb.id)
        assert chunks["test://a"].deleted_at is None
        assert chunks["test://a"].content == "body"


async def test_a_non_empty_prune_records_no_emptied_note(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://b")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://a"))
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert job.stats["scope_pruned"] == 1
        assert "scope_emptied" not in job.stats.get("notes", [])


async def test_the_sweep_reduces_an_emptied_scope_after_the_grace_window(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_on: Settings
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        if await db.get(m.Organization, tenant) is None:
            db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
            await db.flush()
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        kb_id = kb.id
        await _document(db, tenant, kb_id=kb_id, ds_id=ds.id, uri="test://a")
        monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector(listing=_authoritative()))
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb_id)
        assert job.stats["scope_pruned"] == 1

    grace = dt.timedelta(hours=reconcile_on.knowledge_reduce_after_hours)
    await sweep_knowledge_deletions(now=dt.datetime.now(tz=dt.UTC) + grace / 2)
    async with app_session(tenant) as db:
        assert (await _chunks(db, kb_id))["test://a"].reduced_at is None, "inside the window"

    await sweep_knowledge_deletions(now=dt.datetime.now(tz=dt.UTC) + grace + dt.timedelta(hours=1))
    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://a"]
        assert chunk.reduced_at is not None
        assert chunk.content == ""
        await flush_audit(db)
        reduced = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == DOCUMENT_REDUCED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(reduced) == 1
        assert reduced[0].resource["deleted_reason"] == SCOPE_CHANGED
        assert "absent from every attested listing" not in (reduced[0].reason or "")


async def test_reselecting_a_pruned_document_brings_it_back(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """Reduced is not suppressed: the prune is not an erasure order, and the
    cursor eviction means the hash cursor does not skip it either."""
    tenant = uuid.uuid4()
    docs = [_raw(f"test://{n}", f"document {n}") for n in ("a", "b")]
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector(docs=docs))
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        ds.cursor = {**ds.cursor, "scope_changed": True}
        await db.flush()
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://a"))
        )
        pruned = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert pruned.stats["scope_pruned"] == 1

        # Re-selected: the connector yields it again, as a hash-skipping
        # connector does only for digests the cursor no longer holds.
        await db.refresh(ds, ["cursor"])
        assert docs[1].content_hash not in ds.cursor["hashes"]
        monkeypatch.setitem(
            registry._CONNECTORS,
            STUB,
            _Connector(docs=[docs[1]], listing=_authoritative("test://a", "test://b")),
        )
        back = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert back.stats["ingested"] == 1
        assert back.stats["suppressed"] == 0
        live = [
            c
            for c in (
                await db.execute(
                    select(m.KbChunk).where(
                        m.KbChunk.kb_id == kb.id, m.KbChunk.source_uri == "test://b"
                    )
                )
            )
            .scalars()
            .all()
            if c.deleted_at is None
        ]
        assert live and all(c.content == "document b" for c in live)


async def test_a_reduced_prune_is_not_restorable(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """`restore_document` undoes only an unreduced inference. A reduced
    `scope_changed` row has no text to give back, so it answers 0 rather than
    reviving an empty chunk; re-selecting it is the way back."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://b")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://a"))
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        restored = await restore_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://b",
            now=T0,
        )
        assert restored == 0
        assert (await _chunks(db, kb.id))["test://b"].deleted_at is not None


async def _deleted_events(db: Any, tenant: uuid.UUID) -> list[m.AuditEvent]:
    await flush_audit(db)
    return list(
        (
            await db.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == DOCUMENT_DELETED
                )
            )
        )
        .scalars()
        .all()
    )


async def test_a_reduced_prune_names_the_admin_who_narrowed_the_scope(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """Destroying content writes a ledger entry, and it names the admin whose
    edit licensed it -- the sync only carried it out."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": {"by": ADMIN}})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://b")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://a"))
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        events = await _deleted_events(db, tenant)
        assert len(events) == 1
        assert events[0].actor_type == "operator"
        assert str(events[0].actor_id) == ADMIN
        assert (events[0].responsible_type, events[0].responsible_id) == ("operator", ADMIN)
        assert events[0].resource["source_uri"] == "test://b"
        assert events[0].resource["deleted_reason"] == SCOPE_CHANGED
        assert "scope" in (events[0].reason or "")


async def test_a_flag_from_before_attribution_is_recorded_as_the_system(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_off: Settings
) -> None:
    """A bare `True` flag (written before the flag carried its author) still
    prunes; the entry then names nobody it cannot prove."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant, cursor={"scope_changed": True})
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://b")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(listing=_authoritative("test://a"))
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        events = await _deleted_events(db, tenant)
        assert len(events) == 1
        assert events[0].actor_type == "system"
        assert events[0].actor_id is None
        assert events[0].resource["source_uri"] == "test://b"
        assert events[0].resource["deleted_reason"] == SCOPE_CHANGED


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
