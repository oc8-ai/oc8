"""The tick that acts on what a sync observed.

It performs no network IO and calls no connector -- it only reads facts a sync
already wrote -- so everything here is about timing and about transactions. The
grace window is what makes an inference reversible, and the per-document
transaction is what stops the sweep from doing exactly one document per tick and
reporting success: `tenant_session` binds the tenant with a transaction-LOCAL
GUC, and RLS answers an unbound session with silence rather than an error. That
bug nearly shipped in the evidence sweep (tests/evidence/test_sweep.py:415), so
it is pinned here by its own test.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text, update

from oc8 import models as m
from oc8.config import Settings, get_settings
from oc8.knowledge.connectors.base import Attestation, RawDocument, SourceListing
from oc8.knowledge.ingest import ingest_raw_document
from oc8.knowledge.reconcile import observe_listing, sweep_knowledge_deletions
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.knowledge.tombstone import (
    DOCUMENT_REDUCED,
    SUPERSEDED_REDUCED,
    reduce_document,
    restore_document,
    tombstone_document,
)
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

#: The sweep is global -- it walks every tenant in the database, and the test
#: database is shared for the whole session. So the report counters are asserted
#: with `>=` and the EXACT claims are made per tenant, over rows and chain rows
#: this test wrote itself. An exact global count would fail the moment another
#: test in the session left a tombstone whose window had closed.
T0 = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)
PAST_THE_WINDOW = T0 + dt.timedelta(days=8)


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _Attesting:
    """Only the half of a connector the observer uses."""

    def __init__(self, *uris: str) -> None:
        self.uris = frozenset(uris)

    async def attest_listing(self, config: dict[str, Any], auth: Any = None) -> SourceListing:
        return SourceListing(attestation=Attestation.AUTHORITATIVE, present_uris=self.uris)


@pytest.fixture
def reconcile_on(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = get_settings()
    monkeypatch.setattr(settings, "knowledge_reconcile_enabled", True, raising=False)
    return settings


async def _mark_absent(db: Any, *, kb_id: uuid.UUID, uri: str, when: dt.datetime) -> None:
    """Fact A, as a sync's observation writes it.

    `tombstone_document(reason="source_absent")` covers only chunks an attested
    listing actually MARKED -- a row with `missing_since IS NULL` carries its own
    proof that no observation ever missed it. So a test standing in for the sweep
    has to write the mark first, exactly as `observe_listing` does; without this
    the fixture builds a state the product cannot reach.
    """
    await db.execute(
        update(m.KbChunk)
        .where(
            m.KbChunk.kb_id == kb_id,
            m.KbChunk.source_uri == uri,
            m.KbChunk.deleted_at.is_(None),
        )
        .values(missing_since=when)
    )
    await db.flush()


def _raw(uri: str, content: str) -> RawDocument:
    return RawDocument(
        source_uri=uri,
        title=uri,
        content=content,
        content_type="text/plain",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )


def _between_the_work_list_and_the_kill(
    monkeypatch: pytest.MonkeyPatch, app_session: AppSessionFactory, tenant: uuid.UUID, retract: Any
) -> dict[str, int]:
    """Commit `retract` between the sweep's two transactions.

    The work list is read in one transaction that writes nothing, and each
    document is then tombstoned in its OWN transaction. Everything the list was
    built on can be withdrawn in that gap, and this is the gap: the second
    `tenant_session` the sweep opens for this tenant is the first writing one.
    """
    from oc8.knowledge import reconcile as rec

    real = rec.tenant_session
    calls = {"n": 0}

    @contextlib.asynccontextmanager
    async def racing(tenant_id: uuid.UUID) -> Any:
        # Counted per TENANT: the sweep walks every tenant in the shared test
        # database, so "the second session" globally is almost always somebody
        # else's work-list read -- and a retraction that lands before OUR work
        # list is read is not this race at all, it is a work list that never
        # contained the document.
        if tenant_id == tenant:
            calls["n"] += 1
            if calls["n"] == 2:
                async with app_session(tenant) as other:
                    await retract(other)
        async with real(tenant_id) as db:
            yield db

    monkeypatch.setattr(rec, "tenant_session", racing)
    return calls


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
    ds = m.DataSource(
        tenant_id=tenant, connector_type="test_sweep", name="src", config={}, connected=True
    )
    db.add(ds)
    await db.flush()
    return ds


async def _document(
    db: Any, tenant: uuid.UUID, *, kb_id: uuid.UUID, ds_id: uuid.UUID, uri: str, content: str
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


async def _granted_agent(db: Any, tenant: uuid.UUID, kb_id: uuid.UUID) -> m.Agent:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
    db.add(agent)
    await db.flush()
    db.add(
        m.KnowledgeGrant(tenant_id=tenant, kb_id=kb_id, grantee_type="agent", grantee_id=agent.id)
    )
    await db.flush()
    return agent


async def _chunks(db: Any, kb_id: uuid.UUID) -> dict[str, m.KbChunk]:
    rows = (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb_id))).scalars().all()
    return {c.source_uri: c for c in rows}


async def _events(db: Any, tenant: uuid.UUID, action: str) -> list[m.AuditEvent]:
    rows = await db.execute(
        select(m.AuditEvent)
        .where(m.AuditEvent.tenant_id == tenant, m.AuditEvent.action == action)
        .order_by(m.AuditEvent.seq)
    )
    return list(rows.scalars().all())


# ------------------------------------------------------------------ the full path


async def test_a_removed_document_stops_being_retrievable(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: two attested syncs a window apart, then a tick, and the agent
    can no longer be told about a document the customer removed upstream."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        agent = await _granted_agent(db, tenant, kb_id)
        agent_id = agent.id
        for uri, body in (
            ("test://withdrawn", "the withdrawn guidance on parental leave"),
            ("test://current", "the current guidance on parental leave"),
        ):
            await ingest_raw_document(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb_id,
                raw=RawDocument(
                    source_uri=uri,
                    title=uri,
                    content=body,
                    content_type="text/plain",
                    content_hash=uri,
                ),
            )
        for when in (T0, T0 + dt.timedelta(days=4)):
            await observe_listing(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb_id,
                connector=_Attesting("test://current"),
                auth=None,
                fatal=None,
                now=when,
            )

    report = await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=4, minutes=1))

    assert report.documents_tombstoned >= 1
    assert report.chunks >= 1
    async with app_session(tenant) as db:
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        ctx, _ = await retrieve_kb_context(
            db, agent=reloaded, tenant_id=tenant, query_text="guidance on parental leave"
        )
        assert "withdrawn guidance" not in ctx
        assert "current guidance" in ctx


# ------------------------------------------------------------------ reduction


async def test_a_tombstone_is_reduced_only_after_the_grace_window(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """Retention is REDUCTION: the row survives as its own tombstone and the
    digest of what was destroyed lives in the chain. Until the window closes the
    content is still there, which is what makes restore one UPDATE."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        await _document(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds.id,
            uri="test://gone",
            content="the text that is about to become unreadable",
        )
        await _mark_absent(db, kb_id=kb_id, uri="test://gone", when=T0)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds.id,
            source_uri="test://gone",
            reason="source_absent",
            reduce_now=False,
            now=T0,
        )

    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=1))

    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://gone"]
        assert chunk.reduced_at is None
        assert "unreadable" in chunk.content

    late = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert late.documents_reduced >= 1
    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://gone"]
        assert chunk.reduced_at is not None
        assert chunk.content == ""
        assert chunk.embedding is None
        events = await _events(db, tenant, DOCUMENT_REDUCED)
        assert len(events) == 1
        assert events[0].resource["source_uri"] == "test://gone"
        assert events[0].resource["sha256"], "the hash outlives the bytes it names"


async def test_superseded_documents_are_reduced_under_one_aggregate_event(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """Per-document ledger rows for routine re-syncing would grow an append-only
    table without bound as a side effect of ordinary operation -- and each
    append takes the tenant's audit lock, stalling every other audited action in
    that tenant while it runs."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        for i in range(20):
            uri = f"test://superseded-{i}"
            await _document(
                db, tenant, kb_id=kb_id, ds_id=ds.id, uri=uri, content=f"old generation {i}"
            )
            await tombstone_document(
                db,
                tenant_id=tenant,
                kb_id=kb_id,
                data_source_id=ds.id,
                source_uri=uri,
                reason="superseded",
                reduce_now=False,
                now=T0,
            )

    report = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert report.superseded_reduced >= 20
    async with app_session(tenant) as db:
        events = await _events(db, tenant, SUPERSEDED_REDUCED)
        assert len(events) == 1, "one aggregate entry per tenant per tick"
        assert events[0].resource["documents"] == 20
        assert events[0].resource["chunks"] == 20
        assert await _events(db, tenant, DOCUMENT_REDUCED) == []
        left = (
            await db.execute(
                text("SELECT count(*) FROM kb_chunk WHERE content <> '' AND kb_id = :kb"),
                {"kb": kb_id},
            )
        ).scalar_one()
        assert left == 0


async def test_the_ledger_entry_and_the_reduction_commit_together(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strictly stronger than the evidence sweep's commit-then-destroy dance,
    which exists only because ITS destructive step is a filesystem unlink
    outside the database.

    The failure this pins is a commit inserted between the append and the
    UPDATE: the entry would be durable, the tenant GUC would be gone, the UPDATE
    would match zero rows under RLS, and the ledger would carry a permanent,
    unrepairable claim that a document was destroyed while its text sat there.
    So the whole unit is failed after `reduce_document` returns, and BOTH halves
    must be absent afterwards.
    """
    import oc8.knowledge.reconcile as reconcile_mod

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        await _document(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds.id,
            uri="test://atomic",
            content="text that must survive a failed tick",
        )
        await _mark_absent(db, kb_id=kb_id, uri="test://atomic", when=T0)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds.id,
            source_uri="test://atomic",
            reason="source_absent",
            reduce_now=False,
            now=T0,
        )

    real = reconcile_mod.reduce_document

    async def _die_after_reducing(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        raise RuntimeError("the connection dropped before the commit")

    monkeypatch.setattr(reconcile_mod, "reduce_document", _die_after_reducing)
    failed = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert failed.failures >= 1
    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://atomic"]
        assert chunk.reduced_at is None
        assert "must survive" in chunk.content
        assert await _events(db, tenant, DOCUMENT_REDUCED) == []

    monkeypatch.setattr(reconcile_mod, "reduce_document", real)
    ok = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert ok.documents_reduced >= 1
    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://atomic"]
        assert chunk.reduced_at is not None and chunk.content == ""
        assert len(await _events(db, tenant, DOCUMENT_REDUCED)) == 1


async def test_several_documents_are_reduced_in_one_tick(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """The one that nearly shipped broken in the evidence sweep.

    `tenant_session` binds the tenant with a transaction-LOCAL GUC, so the
    commit that makes the FIRST document's ledger entry durable also unbinds the
    session. Every query after it then runs unbound, and RLS answers an unbound
    session with an empty result rather than an error -- so the sweep would
    reduce exactly one document per tenant per tick, for ever, and report
    success. Two tenants, because the same trap sits on the outer loop.
    """
    tenants = [uuid.uuid4(), uuid.uuid4()]
    bases: dict[uuid.UUID, uuid.UUID] = {}
    for tenant in tenants:
        async with app_session(tenant) as db:
            await _org(db, tenant)
            kb = await _kb(db, tenant)
            ds = await _source(db, tenant)
            bases[tenant] = kb.id
            for i in range(3):
                uri = f"test://doc-{i}"
                await _document(db, tenant, kb_id=kb.id, ds_id=ds.id, uri=uri, content=f"body {i}")
                await _mark_absent(db, kb_id=kb.id, uri=uri, when=T0)
                await tombstone_document(
                    db,
                    tenant_id=tenant,
                    kb_id=kb.id,
                    data_source_id=ds.id,
                    source_uri=uri,
                    reason="source_absent",
                    reduce_now=False,
                    now=T0,
                )

    report = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert report.documents_reduced >= 6
    for tenant in tenants:
        async with app_session(tenant) as db:
            chunks = await _chunks(db, bases[tenant])
            assert len(chunks) == 3
            assert all(c.reduced_at is not None and c.content == "" for c in chunks.values())
            assert len(await _events(db, tenant, DOCUMENT_REDUCED)) == 3


async def test_one_bad_document_does_not_stop_the_others(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oc8.knowledge.reconcile as reconcile_mod

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        for uri in ("test://bad", "test://good-1", "test://good-2"):
            await _document(db, tenant, kb_id=kb_id, ds_id=ds.id, uri=uri, content=f"body {uri}")
            await _mark_absent(db, kb_id=kb_id, uri=uri, when=T0)
            await tombstone_document(
                db,
                tenant_id=tenant,
                kb_id=kb_id,
                data_source_id=ds.id,
                source_uri=uri,
                reason="source_absent",
                reduce_now=False,
                now=T0,
            )

    real = reconcile_mod.reduce_document

    async def _one_unhappy_document(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("source_uri") == "test://bad":
            raise RuntimeError("this one document is unhappy")
        return await real(*args, **kwargs)

    monkeypatch.setattr(reconcile_mod, "reduce_document", _one_unhappy_document)
    report = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert report.failures >= 1
    assert report.documents_reduced >= 2
    async with app_session(tenant) as db:
        chunks = await _chunks(db, kb_id)
        assert chunks["test://bad"].reduced_at is None
        assert chunks["test://good-1"].content == ""
        assert chunks["test://good-2"].content == ""


async def test_a_document_already_reduced_by_another_worker_writes_no_second_event(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """`_housekeeping` runs in every worker process, so the claim is re-read
    inside the writing transaction: no live chunks left means somebody else got
    there first, and a second entry would claim a destruction that this tick did
    not perform."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        await _document(
            db, tenant, kb_id=kb_id, ds_id=ds.id, uri="test://once", content="reduced once"
        )
        await _mark_absent(db, kb_id=kb_id, uri="test://once", when=T0)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds.id,
            source_uri="test://once",
            reason="source_absent",
            reduce_now=False,
            now=T0,
        )

    first = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)
    async with app_session(tenant) as db:
        assert len(await _events(db, tenant, DOCUMENT_REDUCED)) == 1

    await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert first.documents_reduced >= 1
    async with app_session(tenant) as db:
        assert len(await _events(db, tenant, DOCUMENT_REDUCED)) == 1, (
            "the second tick found no live chunks to claim, so it wrote nothing"
        )


async def test_the_sweep_is_inert_unless_enabled(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    assert get_settings().knowledge_reconcile_enabled is False
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        await _document(
            db, tenant, kb_id=kb_id, ds_id=ds.id, uri="test://kept", content="still readable"
        )
        await _mark_absent(db, kb_id=kb_id, uri="test://kept", when=T0)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds.id,
            source_uri="test://kept",
            reason="source_absent",
            reduce_now=False,
            now=T0,
        )

    report = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert report.documents_reduced == 0
    assert report.documents_tombstoned == 0
    async with app_session(tenant) as db:
        assert (await _chunks(db, kb_id))["test://kept"].content == "still readable"
        assert await _events(db, tenant, DOCUMENT_REDUCED) == []


# ------------------------------------------------------------------ one generation at a time


async def _generation(
    db: Any,
    tenant: uuid.UUID,
    *,
    kb_id: uuid.UUID,
    ds_id: uuid.UUID,
    uri: str,
    content: str,
    reason: str,
    at: dt.datetime,
) -> None:
    """One tombstoned generation of a document, stamped at a chosen instant.

    Written a generation at a time because `tombstone_document` takes the LIVE
    chunks: creating both rows first would put them both under one reason, which
    is the opposite of what these tests are about.
    """
    await _document(db, tenant, kb_id=kb_id, ds_id=ds_id, uri=uri, content=content)
    if reason == "source_absent":
        await _mark_absent(db, kb_id=kb_id, uri=uri, when=at)
    await tombstone_document(
        db,
        tenant_id=tenant,
        kb_id=kb_id,
        data_source_id=ds_id,
        source_uri=uri,
        reason=reason,
        reduce_now=False,
        now=at,
    )


async def test_a_generation_inside_its_grace_window_survives_the_one_that_expired(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """A document has more than one unreduced generation whenever it was edited
    and then removed upstream, and each one owns its own grace window.

    Measured before `reduce_document` was told which generation the work list
    meant: it re-selected every unreduced chunk of the URI, so the day-0
    `superseded` row coming due destroyed the day-6 `source_absent` row with days
    of its 7-day undo left -- and `restore_document` only reaches
    `reduced_at IS NULL`, so the undo was gone for good.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        await _generation(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds.id,
            uri="test://memo",
            content="the draft the customer replaced",
            reason="superseded",
            at=T0,
        )
        await _generation(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds.id,
            uri="test://memo",
            content="the version that later vanished upstream",
            reason="source_absent",
            at=T0 + dt.timedelta(days=6),
        )

    # Grace is 168h, so at T0+8d only the day-0 generation is due.
    report = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)
    assert report.superseded_reduced >= 1

    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.KbChunk).where(
                        m.KbChunk.kb_id == kb_id, m.KbChunk.source_uri == "test://memo"
                    )
                )
            )
            .scalars()
            .all()
        )
        by_reason = {c.deleted_reason: c for c in rows}
        assert by_reason["superseded"].reduced_at is not None
        assert by_reason["superseded"].content == ""
        assert by_reason["source_absent"].reduced_at is None
        assert by_reason["source_absent"].content == "the version that later vanished upstream"
        assert await _events(db, tenant, DOCUMENT_REDUCED) == [], (
            "nothing absent has been destroyed yet, so nothing may claim it was"
        )


async def test_each_generation_is_reduced_under_its_own_reason(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """Both generations are due in the same tick, and they must not be merged.

    `reduce_document` used to read its reason off whichever row the database
    returned first. Half the time the `source_absent` content was destroyed with
    no `knowledge.document_reduced` entry, no digest and no cursor eviction -- it
    disappeared into the anonymous superseded aggregate -- and the other half the
    ledger's sha256 silently covered superseded text as well. Either way the
    second work-list row then found nothing and was counted as "another worker
    got there first".
    """
    absent_text = "the version that vanished upstream"
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id = kb.id
        await _generation(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds.id,
            uri="test://both",
            content="the draft the customer replaced",
            reason="superseded",
            at=T0,
        )
        await _generation(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds.id,
            uri="test://both",
            content=absent_text,
            reason="source_absent",
            at=T0 + dt.timedelta(hours=1),
        )

    report = await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    assert report.documents_reduced >= 1
    assert report.superseded_reduced >= 1
    async with app_session(tenant) as db:
        reduced = await _events(db, tenant, DOCUMENT_REDUCED)
        assert len(reduced) == 1
        assert reduced[0].resource["chars"] == len(absent_text), (
            "the digest and the char count describe the absent generation and nothing else"
        )
        assert reduced[0].resource["sha256"] == hashlib.sha256(absent_text.encode()).hexdigest()
        aggregate = await _events(db, tenant, SUPERSEDED_REDUCED)
        assert [e.resource["documents"] for e in aggregate] == [1]
        rows = (
            (
                await db.execute(
                    select(m.KbChunk).where(
                        m.KbChunk.kb_id == kb_id, m.KbChunk.source_uri == "test://both"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert all(c.reduced_at is not None and c.content == "" for c in rows)


async def test_a_restore_landing_mid_reduction_leaves_no_claim_in_the_ledger(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim re-read has to be the WRITE, not the read before it.

    `reduce_document` appended `knowledge.document_reduced` and only then ran the
    destroying UPDATE, whose WHERE re-checks `deleted_at IS NOT NULL` precisely
    because a concurrent restore can land in between. Under READ COMMITTED that
    left a permanent, unrepairable entry claiming a destruction that did not
    happen -- `audit_event` has UPDATE and DELETE revoked from `oc8_app` -- and
    `Removal` was returned regardless, so the tick counted it too.

    The restore is driven from inside the one `await` that sits between the
    SELECT and the UPDATE, which is where a concurrent one would commit; the
    rows the UPDATE then sees are exactly the rows it would see in the real race.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        await _document(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds_id,
            uri="test://raced",
            content="text a restore saved in time",
        )
        await _mark_absent(db, kb_id=kb_id, uri="test://raced", when=T0)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds_id,
            source_uri="test://raced",
            reason="source_absent",
            reduce_now=False,
            now=T0,
        )

    async with app_session(tenant) as db:
        real_get = db.get
        restored = False

        async def _restore_mid_flight(*args: Any, **kwargs: Any) -> Any:
            nonlocal restored
            if not restored:
                restored = True
                await restore_document(
                    db,
                    tenant_id=tenant,
                    kb_id=kb_id,
                    data_source_id=ds_id,
                    source_uri="test://raced",
                    now=PAST_THE_WINDOW,
                )
            return await real_get(*args, **kwargs)

        monkeypatch.setattr(db, "get", _restore_mid_flight)
        removal = await reduce_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds_id,
            source_uri="test://raced",
            reason="source_absent",
            deleted_before=PAST_THE_WINDOW - dt.timedelta(days=7),
            now=PAST_THE_WINDOW,
        )
        monkeypatch.undo()

    assert removal is None, "nothing was destroyed, so nothing may be reported as destroyed"
    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://raced"]
        assert chunk.deleted_at is None and chunk.content == "text a restore saved in time"
        assert await _events(db, tenant, DOCUMENT_REDUCED) == []


# ------------------------------------------------------ the stale work list


async def test_a_document_forgiven_after_the_work_list_is_read_is_not_killed(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work list is read in a transaction that writes nothing, and each
    document is tombstoned in its own transaction up to a hundred documents
    later. Everything the list was built on can be retracted in between.

    Measured before the re-read: a sync committing in that window FORGAVE the
    document (`missing_since` back to NULL, because it came back upstream) and
    parked its source `held` (because core had refused the same listing as
    implausible), and the sweep tombstoned it anyway -- on a fact that had been
    withdrawn, seven days before phase 2 would destroy it under an unrepairable
    ledger entry.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        await _document(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds_id,
            uri="test://blipped",
            content="a document that came back before the sweep got to it",
        )
        await _mark_absent(db, kb_id=kb_id, uri="test://blipped", when=T0)
        source = await db.get(m.DataSource, ds_id)
        assert source is not None
        source.last_attested_sync_at = T0 + dt.timedelta(days=4)

    async def forgive(other: Any) -> None:
        """The sync that found it again: forgiven, and its source refused."""
        await other.execute(
            update(m.KbChunk)
            .where(m.KbChunk.kb_id == kb_id, m.KbChunk.source_uri == "test://blipped")
            .values(missing_since=None)
        )
        held = await other.get(m.DataSource, ds_id)
        assert held is not None
        held.reconcile_state = "held"
        held.reconcile_note = "the listing was refused as implausible"

    calls = _between_the_work_list_and_the_kill(monkeypatch, app_session, tenant, forgive)
    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=4, minutes=1))
    monkeypatch.undo()

    assert calls["n"] >= 2, "the race never ran, so this test proves nothing"
    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://blipped"]
        assert chunk.deleted_at is None, "a forgiven document was killed on a retracted fact"
        assert chunk.missing_since is None


async def test_a_document_re_ingested_before_the_kill_keeps_its_fresh_generation(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same window, with the other thing a sync does in it: re-ingest.

    The marked generation becomes `superseded` and a fresh one -- fetched from
    the source seconds earlier, carrying `missing_since IS NULL` -- goes live.
    Measured: the sweep tombstoned THAT one as `source_absent`, so the document
    vanished entirely and the connector's hash cursor then skipped it until the
    reduction a week later evicted its digest.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb_id,
            raw=_raw("test://returned", "generation ONE, which really was absent for a while"),
        )
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb_id,
            raw=_raw("test://kept", "a document that never went anywhere"),
        )
        for when in (T0, T0 + dt.timedelta(days=4)):
            await observe_listing(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb_id,
                connector=_Attesting("test://kept"),
                auth=None,
                fatal=None,
                now=when,
            )

    async def re_ingest(other: Any) -> None:
        source = await other.get(m.DataSource, ds_id)
        assert source is not None
        await ingest_raw_document(
            other,
            tenant_id=tenant,
            data_source=source,
            kb_id=kb_id,
            raw=_raw("test://returned", "generation TWO, freshly fetched and present"),
        )

    calls = _between_the_work_list_and_the_kill(monkeypatch, app_session, tenant, re_ingest)
    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=4, minutes=1))
    monkeypatch.undo()

    assert calls["n"] >= 2, "the race never ran, so this test proves nothing"
    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.KbChunk).where(
                        m.KbChunk.kb_id == kb_id, m.KbChunk.source_uri == "test://returned"
                    )
                )
            )
            .scalars()
            .all()
        )
        live = [r.content for r in rows if r.deleted_at is None]
        assert live == ["generation TWO, freshly fetched and present"], [
            (r.content[:20], r.deleted_reason) for r in rows
        ]


async def test_a_held_source_does_not_have_its_inferred_absences_destroyed(
    app_session: AppSessionFactory, reconcile_on: Settings
) -> None:
    """A hold means core has stopped believing this source's listings. Reducing
    the tombstones those listings produced is rule 1's dangerous half arriving a
    week late, so phase 2 leaves them alone -- still out of retrieval, still
    restorable -- until a human clears the hold. A `superseded` generation is
    exempt: a re-ingest replaced it, which has nothing to do with attestation.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        await _generation(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds_id,
            uri="test://absent",
            content="content a held source must not lose",
            reason="source_absent",
            at=T0,
        )
        await _generation(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds_id,
            uri="test://replaced",
            content="a generation an ordinary re-ingest replaced",
            reason="superseded",
            at=T0,
        )
        source = await db.get(m.DataSource, ds_id)
        assert source is not None
        source.reconcile_state = "held"
        source.reconcile_note = "the connector attested an EMPTY listing"

    await sweep_knowledge_deletions(now=PAST_THE_WINDOW)

    async with app_session(tenant) as db:
        chunks = await _chunks(db, kb_id)
        assert chunks["test://absent"].reduced_at is None
        assert chunks["test://absent"].content == "content a held source must not lose"
        assert chunks["test://replaced"].reduced_at is not None
        assert chunks["test://replaced"].content == ""


async def test_a_source_held_after_the_work_list_is_read_kills_nothing(
    app_session: AppSessionFactory, reconcile_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retraction that does not touch the document at all.

    `reconcile_state = 'ok'` is one of the four predicates that selected this
    document, and the sync that refuses a listing sets it to `held` on the
    SOURCE while every chunk keeps its mark. So the document still looks
    condemned from the row's own point of view -- only the work list's other
    three predicates say otherwise, which is why they are re-read where the
    write happens rather than trusted from a transaction that ended.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        await _document(
            db,
            tenant,
            kb_id=kb_id,
            ds_id=ds_id,
            uri="test://doubted",
            content="content of a source core stopped believing",
        )
        await _mark_absent(db, kb_id=kb_id, uri="test://doubted", when=T0)
        source = await db.get(m.DataSource, ds_id)
        assert source is not None
        source.last_attested_sync_at = T0 + dt.timedelta(days=4)

    async def refuse(other: Any) -> None:
        held = await other.get(m.DataSource, ds_id)
        assert held is not None
        held.reconcile_state = "held"
        held.reconcile_note = "the connector attested an EMPTY listing"

    calls = _between_the_work_list_and_the_kill(monkeypatch, app_session, tenant, refuse)
    await sweep_knowledge_deletions(now=T0 + dt.timedelta(days=4, minutes=1))
    monkeypatch.undo()

    assert calls["n"] >= 2, "the race never ran, so this test proves nothing"
    async with app_session(tenant) as db:
        chunk = (await _chunks(db, kb_id))["test://doubted"]
        assert chunk.deleted_at is None, "a held source lost a document anyway"
        assert chunk.missing_since is not None, "the mark itself must survive the refusal"
