"""What a sync is allowed to conclude from a connector's listing.

Observation stamps facts on rows and never deletes, so every test here is about
the difference between "this document is gone" and "I could not see it this
time". Four roads lead to concluding nothing (no method, `NONE`, an exception, a
timeout) and three refusals stop a listing that is plausible-looking but wrong
from marking anything at all -- because an expired token, a revoked bucket
policy and a folder id that stopped resolving all look exactly like "the
customer deleted everything".

The two-facts rule is the other half: the same witness twice, a confirm window
apart. It defends against transient failure and not at all against systematic
failure, which is what the refusals are for.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.config import Settings, get_settings
from oc8.knowledge.connectors import registry
from oc8.knowledge.connectors.base import (
    Attestation,
    RawDocument,
    SourceListing,
    ValidationResult,
)
from oc8.knowledge.connectors.registry import get_connector
from oc8.knowledge.ingest import run_source_sync
from oc8.knowledge.reconcile import observe_listing, sweep_knowledge_deletions
from oc8.knowledge.tombstone import RECONCILE_HELD
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory
from tests.audit.helpers import flush_audit

pytestmark = pytest.mark.asyncio

STUB = "test_reconcile"
T0 = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _Connector:
    """A connector a test drives completely: what it yields, what it swears to,
    and whether it dies mid-stream."""

    type_id = STUB
    requires_oauth: str | None = None
    label = "Stub"
    description = "test connector"
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(
        self,
        *,
        docs: list[RawDocument] | None = None,
        listing: SourceListing | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.docs = docs or []
        self.listing = listing
        self.fail_after = fail_after

    async def validate(self, config: dict[str, Any], auth: Any = None) -> ValidationResult:
        return ValidationResult(ok=True)

    async def discover(self, config: dict[str, Any], auth: Any = None) -> list[Any]:
        return []

    async def fetch(self, config: dict[str, Any], cursor: Any, auth: Any = None) -> Any:
        for i, doc in enumerate(self.docs):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("the OAuth token expired mid-stream")
            yield doc

    async def attest_listing(self, config: dict[str, Any], auth: Any = None) -> SourceListing:
        assert self.listing is not None, "this connector was not given a listing to swear to"
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
def reconcile_on(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The flag gates observation as well as the sweep, so every test that
    expects a mark has to turn it on."""
    settings = get_settings()
    monkeypatch.setattr(settings, "knowledge_reconcile_enabled", True, raising=False)
    return settings


async def _org(db: Any, tenant: uuid.UUID) -> None:
    if await db.get(m.Organization, tenant) is None:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()


async def _kb(db: Any, tenant: uuid.UUID, name: str = "KB") -> m.KnowledgeBase:
    kb = m.KnowledgeBase(tenant_id=tenant, name=name, embedding_model="nomic-embed-text")
    db.add(kb)
    await db.flush()
    return kb


async def _source(db: Any, tenant: uuid.UUID) -> m.DataSource:
    ds = m.DataSource(tenant_id=tenant, connector_type=STUB, name="src", config={}, connected=True)
    db.add(ds)
    await db.flush()
    return ds


async def _document(
    db: Any,
    tenant: uuid.UUID,
    *,
    kb_id: uuid.UUID,
    ds_id: uuid.UUID | None,
    uri: str,
    content: str = "body",
) -> None:
    db.add(
        m.KbChunk(
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds_id,
            content=content,
            embedding=None,
            source_uri=uri,
            chunk_metadata={"chunk_index": 0},
        )
    )
    await db.flush()


async def _marks(db: Any, kb_id: uuid.UUID) -> dict[str, dt.datetime | None]:
    rows = (
        await db.execute(
            select(m.KbChunk.source_uri, m.KbChunk.missing_since).where(
                m.KbChunk.kb_id == kb_id, m.KbChunk.deleted_at.is_(None)
            )
        )
    ).all()
    return {uri: when for uri, when in rows}


async def _held_events(db: Any, tenant: uuid.UUID) -> list[m.AuditEvent]:
    # Staged events reach the chain at commit; write them so they read back.
    await flush_audit(db)
    rows = await db.execute(
        select(m.AuditEvent).where(
            m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == RECONCILE_HELD
        )
    )
    return list(rows.scalars().all())


# ------------------------------------------------------------------ marking


async def test_a_document_missing_from_an_attested_listing_is_only_marked(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One observation is a suspicion. The content stays exactly where it is."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        for uri in ("test://a", "test://b"):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative("test://b")),
            auth=None,
            fatal=None,
            now=T0,
        )

        assert outcome == "observed"
        marks = await _marks(db, kb.id)
        assert marks["test://a"] == T0
        assert marks["test://b"] is None
        rows = (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars().all()
        assert all(c.deleted_at is None for c in rows), "marking is not killing"
        assert all(c.content == "body" for c in rows)
        assert ds.last_attested_sync_at == T0


async def test_two_attested_syncs_a_window_apart_are_required(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """Fact A and fact B, in one SQL comparison.

    After a single attested sync `last_attested_sync_at` EQUALS `missing_since`,
    so no amount of waiting satisfies the rule -- what is required is a second
    attested sync, at least the confirm window later, that also did not contain
    the document. Design-3-as-reviewed had these as separate predicates with no
    minimum separation, which let two syncs a minute apart count as two
    observations that agree.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        for uri in ("test://gone", "test://kept"):
            await _document(db, tenant, kb_id=kb_id, ds_id=ds.id, uri=uri)
        await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb_id,
            connector=_Connector(listing=_authoritative("test://kept")),
            auth=None,
            fatal=None,
            now=T0,
        )
        assert ds.last_attested_sync_at == T0 == (await _marks(db, kb_id))["test://gone"]

    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=30))
    async with app_session(tenant) as db:
        still_there = (
            (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb_id))).scalars().all()
        )
        assert all(c.deleted_at is None for c in still_there), "waiting is not a second observation"

    async with app_session(tenant) as db:
        ds = (await db.execute(select(m.DataSource))).scalars().one()
        await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb_id,
            connector=_Connector(listing=_authoritative("test://kept")),
            auth=None,
            fatal=None,
            now=T0 + dt.timedelta(days=4),
        )

    confirmed = await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=4, minutes=1))

    # The sweep walks every tenant in a database this session shares, so the
    # exact claims are made over this tenant's own rows; the counter only has to
    # agree that something happened.
    assert confirmed.documents_tombstoned >= 1
    async with app_session(tenant) as db:
        rows = {
            c.source_uri: c
            for c in (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb_id)))
            .scalars()
            .all()
        }
        assert rows["test://gone"].deleted_at is not None
        assert rows["test://gone"].deleted_reason == "source_absent"
        assert rows["test://gone"].reduced_at is None, "still inside the grace window"
        assert rows["test://kept"].deleted_at is None


async def test_two_attested_syncs_inside_the_window_kill_nothing(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """The permissions-blip shape.

    Two syncs a minute apart during a one-minute outage are not two independent
    observations that agree -- the separation is the whole content of fact B.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        await _document(db, tenant, kb_id=kb_id, ds_id=ds.id, uri="test://blip")
        # The listing is not empty -- an empty one against a non-empty baseline
        # would be refused for a different reason, and this test is about the
        # window, not about the refusals.
        await _document(db, tenant, kb_id=kb_id, ds_id=ds.id, uri="test://kept")
        listing = _authoritative("test://kept")
        for when in (T0, T0 + dt.timedelta(minutes=1)):
            await observe_listing(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb_id,
                connector=_Connector(listing=listing),
                auth=None,
                fatal=None,
                now=when,
            )

    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=30))

    async with app_session(tenant) as db:
        rows = (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb_id))).scalars().all()
        assert all(c.deleted_at is None for c in rows)


async def test_a_failed_sync_is_discarded(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """Gated on the connector-level fatal, not on the job status: a sync that
    died halfway saw a folder it could not finish reading."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        ds.last_attested_sync_at = T0
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative()),
            auth=None,
            fatal="the token expired",
            now=T0 + dt.timedelta(days=5),
        )

        assert outcome == "failed_sync"
        assert (await _marks(db, kb.id))["test://a"] is None
        assert ds.last_attested_sync_at == T0


async def test_a_failed_sync_advances_nothing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendment A2. The bookkeeping used to sit OUTSIDE the try.

    So an expired token advanced the cursor exactly as if the sync had
    succeeded, and every document the connector never got to was then skipped by
    its own hash cursor on the next run. The job records `failed` with its
    error -- that is the record of what happened; `doc_count` for whatever did
    land before the failure is recovered by the next successful sync, which
    counts rows rather than trusting a counter.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    first = _raw("test://one", "the document that was already there")
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(registry._CONNECTORS, STUB, _Connector(docs=[first]))
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        await db.refresh(ds, ["cursor", "last_sync_at", "doc_count"])
        cursor_before = dict(ds.cursor)
        last_sync_before = ds.last_sync_at
        doc_count_before = ds.doc_count

        second = _raw("test://two", "a document that landed before the failure")
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _Connector(docs=[second, first], fail_after=1)
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.status == "failed"
        assert job.stats["error"]
        await db.refresh(ds, ["cursor", "last_sync_at", "doc_count", "last_attested_sync_at"])
        assert dict(ds.cursor) == cursor_before
        assert ds.last_sync_at == last_sync_before
        assert ds.doc_count == doc_count_before
        assert ds.last_attested_sync_at is None


async def test_a_partial_sync_still_reconciles(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction health is not listing completeness.

    `job.status == "partial"` is set by ONE unsupported content type among a
    folder of documents. Treating that as a reason to stop reconciling would
    disable deletion propagation for that source permanently and invisibly.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    good = _raw("test://good", "a document that parses")
    broken = RawDocument(
        source_uri="test://broken",
        title="b",
        content="\x00",
        content_type="application/octet-stream",
        content_hash="x",
    )
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://vanished")
        monkeypatch.setitem(
            registry._CONNECTORS,
            STUB,
            _Connector(docs=[good, broken], listing=_authoritative("test://good")),
        )

        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.status == "partial"
        assert job.stats["reconcile"] == "observed"
        assert (await _marks(db, kb.id))["test://vanished"] is not None


# ------------------------------------------------------------------ refusals


async def test_an_empty_attested_listing_is_held(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """An expired token and an emptied folder are the same wire response.

    The customer who really did delete everything has a route for it, and the
    note has to say so or an operator reads the hold as a bug in the sweep.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        for uri in ("test://a", "test://b"):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative()),
            auth=None,
            fatal=None,
            now=T0,
        )

        assert outcome == "held_empty"
        assert ds.reconcile_state == "held"
        assert ds.reconcile_note
        assert "/knowledge/sources" in ds.reconcile_note, (
            "the note must name the endpoint that really does empty a source"
        )
        assert ds.last_attested_sync_at is None
        assert set((await _marks(db, kb.id)).values()) == {None}
        assert len(await _held_events(db, tenant)) == 1


async def test_a_disjoint_listing_is_held(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """URI-construction drift: the listing is perfectly valid and describes a
    different namespace (bare S3 keys against `s3://bucket/key` chunks). One
    check turns a plugin bug into a log line instead of a wiped corpus."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        for uri in ("s3://bucket/a.txt", "s3://bucket/b.txt"):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative("a.txt", "b.txt")),
            auth=None,
            fatal=None,
            now=T0,
        )

        assert outcome.startswith("held")
        assert ds.reconcile_state == "held"
        assert ds.reconcile_note
        assert set((await _marks(db, kb.id)).values()) == {None}
        assert len(await _held_events(db, tenant)) == 1


async def test_more_than_half_a_source_vanishing_at_once_is_held(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        uris = [f"test://doc-{i}" for i in range(10)]
        for uri in uris:
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative(*uris[:4])),
            auth=None,
            fatal=None,
            now=T0,
        )

        assert outcome == "held_fraction"
        assert ds.reconcile_state == "held"
        assert set((await _marks(db, kb.id)).values()) == {None}, "nothing at all is marked"


async def test_the_hold_is_computed_over_newly_missing_documents_only(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """With the cumulative form the missing set only ever grows -- documents
    leave the baseline only when the sweep kills them, and the sweep is off by
    default -- so a source that loses 20% now and 40% next month would latch to
    `held` and never clear again."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        uris = [f"test://doc-{i}" for i in range(10)]
        for uri in uris:
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)

        first = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative(*uris[2:])),
            auth=None,
            fatal=None,
            now=T0,
        )
        second = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative(*uris[4:])),
            auth=None,
            fatal=None,
            now=T0 + dt.timedelta(days=4),
        )

        assert first == "observed" and second == "observed"
        assert ds.reconcile_state == "ok"
        marks = await _marks(db, kb.id)
        assert marks["test://doc-0"] == T0 and marks["test://doc-1"] == T0
        assert marks["test://doc-2"] == T0 + dt.timedelta(days=4)
        assert marks["test://doc-3"] == T0 + dt.timedelta(days=4)
        assert marks["test://doc-9"] is None


async def test_a_two_document_source_losing_one_is_not_held(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """Ratios behave badly at small N, so there is a floor: a two-document
    source losing its second is not evidence of a credential failure."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        for uri in ("test://a", "test://b"):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative("test://a")),
            auth=None,
            fatal=None,
            now=T0,
        )

        assert outcome == "observed"
        assert ds.reconcile_state == "ok"
        assert (await _marks(db, kb.id))["test://b"] == T0


# ------------------------------------------------------------------ forgiving


async def test_a_document_that_reappears_is_forgiven(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """A document that comes back is not dying. Only AUTHORITATIVE forgives,
    because only an authoritative listing can assert presence."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        for uri in ("test://gone", "test://kept"):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)

        await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative("test://kept")),
            auth=None,
            fatal=None,
            now=T0,
        )
        assert (await _marks(db, kb.id))["test://gone"] == T0

        await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative("test://gone", "test://kept")),
            auth=None,
            fatal=None,
            now=T0 + dt.timedelta(days=5),
        )
        assert (await _marks(db, kb.id))["test://gone"] is None

    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=60))

    async with app_session(tenant) as db:
        rows = (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars().all()
        assert all(c.deleted_at is None for c in rows)


async def test_a_removals_attestation_marks_exactly_what_it_names(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """A REMOVALS listing cannot assert presence, so absence from it means
    nothing and it never forgives an earlier mark."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        for uri in ("test://named", "test://unlisted", "test://already-marked"):
            await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri)
        await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative("test://named", "test://unlisted")),
            auth=None,
            fatal=None,
            now=T0,
        )
        assert (await _marks(db, kb.id))["test://already-marked"] == T0

        await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(
                listing=SourceListing(
                    attestation=Attestation.REMOVALS,
                    removed_uris=frozenset({"test://named"}),
                )
            ),
            auth=None,
            fatal=None,
            now=T0 + dt.timedelta(days=4),
        )

        marks = await _marks(db, kb.id)
        assert marks["test://named"] == T0 + dt.timedelta(days=4)
        assert marks["test://unlisted"] is None, "absence from a REMOVALS listing means nothing"
        assert marks["test://already-marked"] == T0, "a REMOVALS listing never forgives"


# ------------------------------------------------------------------ silence


async def test_a_connector_that_cannot_attest_never_loses_a_document(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """The real website connector has no `attest_listing` member, and the whole
    seam is duck-typed, so it means NONE. It passes silently over every fetch
    failure -- a 404, a timeout, a 500 and a deleted page are one observation --
    which is why it must never become a deletion authority."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="https://example.com/page")
        website = get_connector("website")
        assert getattr(website, "attest_listing", None) is None

        for day in range(10):
            outcome = await observe_listing(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb.id,
                connector=website,
                auth=None,
                fatal=None,
                now=T0 + dt.timedelta(days=day),
            )
            assert outcome == "no_attestation"

        assert (await _marks(db, kb.id))["https://example.com/page"] is None
        assert ds.last_attested_sync_at is None
        assert ds.reconcile_state == "ok"


async def test_legacy_chunks_are_adopted_by_an_attested_listing(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """Without adoption the slice is inert on every existing corpus.

    The 0045 backfill can only attribute `upload://` URIs, and the hash cursor
    means an unchanged document is never re-yielded -- so a pre-0045 Drive chunk
    would never acquire a source id on any future sync either. An attested
    listing naming the URI is exactly the evidence that authorises it, and it is
    a fact rather than the created_at guess the migration refused to make.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        await _document(db, tenant, kb_id=kb.id, ds_id=None, uri="test://named-legacy")
        await _document(db, tenant, kb_id=kb.id, ds_id=None, uri="test://other-legacy")
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://known")

        await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative("test://named-legacy", "test://known")),
            auth=None,
            fatal=None,
            now=T0,
        )

        rows = {
            c.source_uri: c
            for c in (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id)))
            .scalars()
            .all()
        }
        assert rows["test://named-legacy"].data_source_id == ds.id
        assert rows["test://other-legacy"].data_source_id is None
        assert rows["test://other-legacy"].missing_since is None, (
            "an unattributed row is outside the mechanism entirely"
        )


async def test_observation_is_inert_unless_enabled(app_session: AppSessionFactory) -> None:
    """Gating only the sweep would leave the feature writing marks and held
    state into an append-only ledger while nothing could ever act on them."""
    tenant = uuid.uuid4()
    assert get_settings().knowledge_reconcile_enabled is False
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri="test://a")

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            connector=_Connector(listing=_authoritative()),
            auth=None,
            fatal=None,
            now=T0,
        )

        assert outcome == "disabled"
        assert (await _marks(db, kb.id))["test://a"] is None
        assert ds.reconcile_state == "ok"
        assert ds.last_attested_sync_at is None
        assert await _held_events(db, tenant) == []


# ------------------------------------------------------------------ one source, several bases


async def test_an_empty_listing_is_refused_in_whichever_base_it_arrives_in(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """A source legitimately feeds several bases (§2.1), and the listing is a
    claim about the SOURCE, not about one base's slice of it.

    Measured with the baseline scoped to `kb_id`: an empty AUTHORITATIVE listing
    -- the wire shape of a Drive folder id that stopped resolving, HTTP 200 with
    `files: []` -- was refused when the source synced base A, and silently
    ACCEPTED when the same source synced base B, where it had no live chunks
    yet. Accepted means it stamped `last_attested_sync_at`, which is fact B, a
    column on `data_source` that the sweep joins with no kb predicate. So a
    document in base A died on ONE real observation plus a listing that had never
    been compared against anything -- both §4.2's two-facts rule and the
    empty-listing refusal bypassed at once.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb_a = await _kb(db, tenant, "A")
        kb_b = await _kb(db, tenant, "B")
        ds = await _source(db, tenant)
        for uri in ("test://one", "test://two", "test://three", "test://gone"):
            await _document(db, tenant, kb_id=kb_a.id, ds_id=ds.id, uri=uri)

        # One honest listing against base A: test://gone really is absent, so it
        # is marked and nothing more.
        assert (
            await observe_listing(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb_a.id,
                connector=_Connector(
                    listing=_authoritative("test://one", "test://two", "test://three")
                ),
                auth=None,
                fatal=None,
                now=T0,
            )
            == "observed"
        )
        first_attested = ds.last_attested_sync_at

        outcome = await observe_listing(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb_b.id,
            connector=_Connector(listing=_authoritative()),
            auth=None,
            fatal=None,
            now=T0 + dt.timedelta(days=4),
        )

        assert outcome == "held_empty", "the base being synced does not change what was attested"
        assert ds.reconcile_state == "held"
        assert ds.last_attested_sync_at == first_attested, "a refused listing is not fact B"

    report = await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=4, minutes=1))
    assert report.failures == 0

    async with app_session(tenant) as db:
        rows = (
            await db.execute(
                select(m.KbChunk.source_uri, m.KbChunk.deleted_at).where(m.KbChunk.kb_id == kb_a.id)
            )
        ).all()
        assert [r.source_uri for r in rows if r.deleted_at is not None] == [], (
            "one honest observation plus an empty listing is not two facts that agree"
        )


async def test_a_second_attested_sync_into_another_base_still_confirms(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """The other direction of the same scope, so the fix is not just "refuse
    more".

    Fact B is a property of the source, so a genuine second listing -- one that
    enumerates what the source still holds and passes every refusal -- confirms a
    mark no matter which base the sync that carried it was writing into.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb_a = await _kb(db, tenant, "A")
        kb_b = await _kb(db, tenant, "B")
        ds = await _source(db, tenant)
        for uri in ("test://kept", "test://gone"):
            await _document(db, tenant, kb_id=kb_a.id, ds_id=ds.id, uri=uri)

        honest = _Connector(listing=_authoritative("test://kept"))
        for kb_id, when in ((kb_a.id, T0), (kb_b.id, T0 + dt.timedelta(days=4))):
            assert (
                await observe_listing(
                    db,
                    tenant_id=tenant,
                    data_source=ds,
                    kb_id=kb_id,
                    connector=honest,
                    auth=None,
                    fatal=None,
                    now=when,
                )
                == "observed"
            )
        assert ds.last_attested_sync_at == T0 + dt.timedelta(days=4)

    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=4, minutes=1))

    async with app_session(tenant) as db:
        rows = dict(
            (
                await db.execute(
                    select(m.KbChunk.source_uri, m.KbChunk.deleted_reason).where(
                        m.KbChunk.kb_id == kb_a.id
                    )
                )
            ).all()
        )
        assert rows["test://gone"] == "source_absent"
        assert rows["test://kept"] is None
