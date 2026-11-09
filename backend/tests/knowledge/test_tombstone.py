"""Taking a document back out of a knowledge base, and proving it.

The dangerous half of this file is not that a deletion happens -- it is that a
deletion can be *claimed*. So most of what is asserted here is evidence: the
content is unreadable by a query the customer could run themselves, the ledger
names what was destroyed, the chain still verifies, and a second delete of the
same document does not extend the chain with a second claim it cannot repair.

The other half is the two reversible cases. A re-ingest supersedes the
generation it replaces and an inferred absence is tombstoned, both keeping their
content for the grace window -- and a re-ingest that FAILS touches neither.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.audit.chain import verify_chain
from oc8.auth import get_identity_provider
from oc8.knowledge.chunks import list_documents
from oc8.knowledge.connectors import registry
from oc8.knowledge.connectors.base import RawDocument, ValidationResult
from oc8.knowledge.ingest import ingest_document, ingest_raw_document, run_source_sync
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.knowledge.tombstone import (
    BASE_DELETED,
    DOCUMENT_DELETED,
    DOCUMENT_RESTORED,
    SOURCE_DELETED,
    SOURCE_UNLINKED_FROM_BASE,
    Removal,
    recompute_freshness,
    reduce_document,
    restore_document,
    suppressed_uris,
    tombstone_base,
    tombstone_document,
    tombstone_source,
    unlink_source_from_base,
)
from oc8.main import create_app
from oc8.models.knowledge import EMBED_DIM
from tests.audit.helpers import flush_audit
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

STUB = "test_tombstone"


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _StubConnector:
    """A connector that yields exactly what a test hands it.

    `honour_cursor` mirrors what the three in-tree connectors do -- skip a
    document whose content digest is already in `cursor["hashes"]` -- because
    cursor eviction is only observable through a connector that consults it.
    """

    type_id = STUB
    requires_oauth: str | None = None
    label = "Stub"
    description = "test connector"
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, docs: list[RawDocument], *, honour_cursor: bool = False) -> None:
        self.docs = docs
        self.honour_cursor = honour_cursor

    async def validate(self, config: dict[str, Any], auth: Any = None) -> ValidationResult:
        return ValidationResult(ok=True)

    async def discover(self, config: dict[str, Any], auth: Any = None) -> list[Any]:
        return []

    async def fetch(self, config: dict[str, Any], cursor: Any, auth: Any = None) -> Any:
        seen = set((cursor or {}).get("hashes", []))
        for doc in self.docs:
            if self.honour_cursor and doc.content_hash in seen:
                continue
            yield doc


def _raw(uri: str, content: str, *, content_type: str = "text/plain") -> RawDocument:
    return RawDocument(
        source_uri=uri,
        title=uri,
        content=content,
        content_type=content_type,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


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


async def _org(db: Any, tenant: uuid.UUID) -> None:
    if await db.get(m.Organization, tenant) is None:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()


async def _kb(db: Any, tenant: uuid.UUID, name: str = "KB") -> m.KnowledgeBase:
    kb = m.KnowledgeBase(
        tenant_id=tenant, name=name, embedding_model="nomic-embed-text", classification="internal"
    )
    db.add(kb)
    await db.flush()
    return kb


async def _source(db: Any, tenant: uuid.UUID, *, connector_type: str = STUB) -> m.DataSource:
    ds = m.DataSource(
        tenant_id=tenant, connector_type=connector_type, name="src", config={}, connected=True
    )
    db.add(ds)
    await db.flush()
    return ds


async def _granted_agent(db: Any, tenant: uuid.UUID, kb_id: uuid.UUID) -> m.Agent:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
    db.add(agent)
    await db.flush()
    db.add(
        m.KnowledgeGrant(tenant_id=tenant, kb_id=kb_id, grantee_type="agent", grantee_id=agent.id)
    )
    await db.flush()
    return agent


async def _live(db: Any, kb_id: uuid.UUID, uri: str) -> list[m.KbChunk]:
    rows = await db.execute(
        select(m.KbChunk).where(
            m.KbChunk.kb_id == kb_id,
            m.KbChunk.source_uri == uri,
            m.KbChunk.deleted_at.is_(None),
        )
    )
    return list(rows.scalars().all())


async def _all_chunks(db: Any, kb_id: uuid.UUID, uri: str) -> list[m.KbChunk]:
    rows = await db.execute(
        select(m.KbChunk).where(m.KbChunk.kb_id == kb_id, m.KbChunk.source_uri == uri)
    )
    return list(rows.scalars().all())


async def _events(db: Any, tenant: uuid.UUID) -> list[m.AuditEvent]:
    # Staged events reach the chain at commit; write them so they read back.
    await flush_audit(db)
    rows = await db.execute(
        select(m.AuditEvent)
        .where(m.AuditEvent.tenant_id == tenant, m.AuditEvent.category == "knowledge")
        .order_by(m.AuditEvent.seq)
    )
    return list(rows.scalars().all())


# ------------------------------------------------------------------ erasure


async def test_gdpr_erasure_receipt(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE acceptance test: the customer's own question, answered five ways.

    "Prove it is gone" must be answerable by a query over the table, by the
    retrieval path an agent actually uses, and by a ledger entry that names the
    document and survives chain verification -- addressable by the receipt the
    delete call handed back. Any one of those alone is a claim.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    sentence = "the reorganisation of the Braunschweig depot begins in March"

    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        kb_id = kb.id
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            filename="memo.txt",
            content=sentence,
            content_type="text/plain",
        )
        agent = await _granted_agent(db, tenant, kb_id)
        agent_id = agent.id
        uri = (
            (await db.execute(select(m.KbChunk.source_uri).where(m.KbChunk.kb_id == kb_id)))
            .scalars()
            .first()
        )
        assert uri is not None
        ctx, _ = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="Braunschweig depot"
        )
        assert sentence in ctx

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.request(
                "DELETE",
                f"/api/v1/knowledge/bases/{kb_id}/documents",
                params={"sourceUri": uri},
                headers=_headers(tenant),
            )
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["chunks"] >= 1
    assert receipt["sha256"]
    audit_seq = receipt["auditSeq"]

    async with app_session(tenant) as db:
        readable = (
            await db.execute(
                text("SELECT count(*) FROM kb_chunk WHERE content LIKE :pat"),
                {"pat": f"%{sentence}%"},
            )
        ).scalar_one()
        assert readable == 0, "the sentence must not be readable anywhere in the table"

        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        ctx, _ = await retrieve_kb_context(
            db, agent=reloaded, tenant_id=tenant, query_text="Braunschweig depot"
        )
        assert ctx == ""

        events = await _events(db, tenant)
        assert [e.action for e in events] == [DOCUMENT_DELETED]
        resource = events[0].resource
        assert resource["source_uri"] == uri
        assert resource["chunks"] == receipt["chunks"]
        assert resource["sha256"] == receipt["sha256"]
        assert events[0].seq == audit_seq, "the receipt must address the entry it claims"
        assert await verify_chain(db, tenant) is True


async def test_a_reduced_chunk_that_still_holds_content_cannot_be_written(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant that makes the erasure provable without reading the code.

    An UPDATE that stamps `reduced_at` and forgets to empty the row fails at the
    moment of the mistake rather than a year later, in a DSR response.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        await ingest_raw_document(
            db, tenant_id=tenant, data_source=ds, kb_id=kb.id, raw=_raw("test://1", "still here")
        )
        chunk_id = (await _live(db, kb.id, "test://1"))[0].id

    async with app_session(tenant) as db:
        with pytest.raises(IntegrityError) as exc:
            await db.execute(
                text(
                    "UPDATE kb_chunk SET deleted_at = now(), "
                    "deleted_reason = 'operator_delete', reduced_at = now() WHERE id = :id"
                ),
                {"id": chunk_id},
            )
        assert "ck_kb_chunk_reduced_is_empty" in str(exc.value)
        await db.rollback()


async def test_deleting_twice_writes_one_ledger_entry(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`audit_event` has UPDATE and DELETE revoked from `oc8_app`, so a second
    entry claiming a deletion that did not happen could never be repaired."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        kb_id = kb.id
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            filename="a.txt",
            content="delete me twice",
            content_type="text/plain",
        )
        uri = (await db.execute(select(m.KbChunk.source_uri))).scalars().first()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            first = await c.request(
                "DELETE",
                f"/api/v1/knowledge/bases/{kb_id}/documents",
                params={"sourceUri": uri},
                headers=_headers(tenant),
            )
            assert first.status_code == 200, first.text
            second = await c.request(
                "DELETE",
                f"/api/v1/knowledge/bases/{kb_id}/documents",
                params={"sourceUri": uri},
                headers=_headers(tenant),
            )
    assert second.status_code == 404, second.text
    async with app_session(tenant) as db:
        assert len(await _events(db, tenant)) == 1


async def test_deleting_a_never_ingested_uri_is_a_404_and_writes_nothing(
    app_session: AppSessionFactory,
) -> None:
    """A 204 on a URI nobody ever ingested would make "it's gone" and "you typed
    it wrong" the same answer -- and would append a permanent claim about a
    document that never existed."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        kb_id = kb.id

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.request(
                "DELETE",
                f"/api/v1/knowledge/bases/{kb_id}/documents",
                params={"sourceUri": "upload://never/ingested.txt"},
                headers=_headers(tenant),
            )
    assert r.status_code == 404, r.text
    async with app_session(tenant) as db:
        assert await _events(db, tenant) == []


# ------------------------------------------------------------------ supersede


async def test_a_superseded_document_loses_its_old_chunks_but_keeps_its_content_for_the_window(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        agent = await _granted_agent(db, tenant, kb.id)
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            raw=_raw("test://policy", "the old travel policy allows business class"),
        )
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            raw=_raw("test://policy", "the new travel policy allows economy only"),
        )

        rows = await _all_chunks(db, kb.id, "test://policy")
        dead = [c for c in rows if c.deleted_at is not None]
        live = [c for c in rows if c.deleted_at is None]
        assert dead and live
        assert all(c.deleted_reason == "superseded" for c in dead)
        assert all(c.reduced_at is None for c in dead), "reversible for the grace window"
        assert any("business class" in c.content for c in dead), "content is retained"
        assert all("economy only" in c.content for c in live)

        ctx, _ = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="travel policy"
        )
        assert "business class" not in ctx
        assert "economy only" in ctx


async def test_a_changed_document_leaves_one_live_generation_with_no_flag_set(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendment A3. Superseding has no feature flag, on purpose.

    A flag defaulting off would leave the live defect in place: today an edited
    document ingests beside its previous version and the agent answers from
    both, with no way to tell which is current. The tombstone is reversible by
    construction, so the repo's default-off habit for destructive switches does
    not apply to it -- only to the reduction that follows.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    from oc8.config import get_settings

    assert not hasattr(get_settings(), "knowledge_supersede_enabled"), (
        "A3: there is no supersede flag to set"
    )
    assert get_settings().knowledge_reconcile_enabled is False, "and the sweep stays off"

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        agent = await _granted_agent(db, tenant, kb.id)
        for body in ("draft one of the price list", "draft two of the price list"):
            await ingest_raw_document(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb.id,
                raw=_raw("test://prices", body),
            )

        live = await _live(db, kb.id, "test://prices")
        assert live, "the current generation is still there"
        assert {c.content for c in live} == {"draft two of the price list"}
        ctx, _ = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="price list"
        )
        assert "draft one" not in ctx


async def test_a_document_that_fails_to_parse_keeps_its_old_chunks(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the safety property, so it is pinned rather than implied.

    A document whose upstream version became unparseable raises inside the
    ingest tail, is caught per-document by `run_source_sync`, and its previous
    generation is never touched. Superseding after the new chunks are written is
    what makes that true.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    good = _raw("test://spec", "the readable first version")
    broken = RawDocument(
        source_uri="test://spec",
        title="spec",
        content="\x00 not a document",
        content_type="application/octet-stream",
        content_hash="broken",
    )
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(registry._CONNECTORS, STUB, _StubConnector([good]))
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert len(await _live(db, kb.id, "test://spec")) >= 1

        monkeypatch.setitem(registry._CONNECTORS, STUB, _StubConnector([broken]))
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        assert job.status == "failed"
        live = await _live(db, kb.id, "test://spec")
        assert [c.content for c in live] == ["the readable first version"]


async def test_supersede_adopts_legacy_chunks_instead_of_duplicating_them(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-0045 chunk has no `data_source_id`, so an identity that insisted on
    one would leave the old generation live beside the new one for ever."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        db.add(
            m.KbChunk(
                tenant_id=tenant,
                kb_id=kb.id,
                content="the legacy generation",
                embedding=None,
                source_uri="test://legacy",
                chunk_metadata={"chunk_index": 0},
            )
        )
        await db.flush()

        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            raw=_raw("test://legacy", "the current generation"),
        )

        rows = await _all_chunks(db, kb.id, "test://legacy")
        legacy = [c for c in rows if c.content == "the legacy generation"]
        assert len(legacy) == 1
        assert legacy[0].deleted_at is not None
        assert legacy[0].deleted_reason == "superseded"
        assert legacy[0].data_source_id == ds.id, "the re-ingest is what attributes it"
        assert {c.content for c in rows if c.deleted_at is None} == {"the current generation"}


# ------------------------------------------------------------------ suppression


async def test_an_operator_deleted_document_is_not_re_ingested_by_the_next_sync(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An erasure that the next sync undoes is not an erasure.

    The write path consults the operator's own tombstones, so this holds even
    for a connector that ignores the cursor entirely -- which `upload.py`
    already does and any future delta-token connector would too.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    doc = _raw("test://erased", "content the customer asked us to erase")
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(registry._CONNECTORS, STUB, _StubConnector([doc]))
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert await _live(db, kb.id, "test://erased")

        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://erased",
            reason="operator_delete",
            reduce_now=True,
            now=dt.datetime.now(tz=dt.UTC),
        )
        assert await suppressed_uris(
            db, tenant_id=tenant, kb_id=kb.id, data_source_id=ds.id
        ) == frozenset({"test://erased"})

        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert job.stats["suppressed"] == 1
        assert await _live(db, kb.id, "test://erased") == []


# ------------------------------------------------------------------ the cursor


async def test_a_source_absent_document_can_be_ingested_again(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without cursor eviction a document restored upstream could never come
    back: its digest would still be in `hashes` and the connector would skip
    it for ever."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    doc = _raw("test://back", "a document that came back")
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(registry._CONNECTORS, STUB, _StubConnector([doc], honour_cursor=True))
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert doc.content_hash in ds.cursor["hashes"]

        now = dt.datetime.now(tz=dt.UTC)
        await _mark_absent(db, kb_id=kb.id, uri="test://back", when=now)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://back",
            reason="source_absent",
            reduce_now=False,
            now=now,
        )
        await reduce_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://back",
            reason="source_absent",
            # The sweep names the generation it means and the window it has
            # cleared; `reduce_document` re-applies both rather than trusting
            # the caller's word for them.
            deleted_before=now + dt.timedelta(seconds=1),
            now=now,
        )
        await db.refresh(ds, ["cursor"])
        assert doc.content_hash not in ds.cursor.get("hashes", [])
        assert "test://back" not in ds.cursor.get("uri_hashes", {})

        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        live = await _live(db, kb.id, "test://back")
        assert [c.content for c in live] == ["a document that came back"]


async def test_a_shared_digest_is_not_evicted_while_another_uri_still_uses_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two documents with identical bytes share one digest, and `hashes` holds
    bare digests -- evicting on the first reduction would make the second
    document re-ingest on every sync for ever."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    body = "the same bytes under two names"
    twins = [_raw("test://twin-a", body), _raw("test://twin-b", body)]
    assert twins[0].content_hash == twins[1].content_hash
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(registry._CONNECTORS, STUB, _StubConnector(twins))
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        now = dt.datetime.now(tz=dt.UTC)
        await _mark_absent(db, kb_id=kb.id, uri="test://twin-a", when=now)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://twin-a",
            reason="source_absent",
            reduce_now=False,
            now=now,
        )
        await reduce_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://twin-a",
            reason="source_absent",
            deleted_before=now + dt.timedelta(seconds=1),
            now=now,
        )
        await db.refresh(ds, ["cursor"])

        assert twins[0].content_hash in ds.cursor.get("hashes", [])
        assert "test://twin-a" not in ds.cursor.get("uri_hashes", {})
        assert "test://twin-b" in ds.cursor.get("uri_hashes", {})


# ------------------------------------------------------------------ restore


async def test_restore_brings_back_an_inferred_deletion_and_refuses_a_reduced_one(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 2 made concrete: the machine's guesses are reversible, a human's
    command is not, and reduction is where the difference stops being a
    policy and starts being a fact about the row."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        agent = await _granted_agent(db, tenant, kb_id)
        agent_id = agent.id
        for uri, body in (
            ("test://recoverable", "the recoverable minutes of the board meeting"),
            ("test://reduced", "the reduced minutes of the board meeting"),
        ):
            await ingest_raw_document(
                db, tenant_id=tenant, data_source=ds, kb_id=kb_id, raw=_raw(uri, body)
            )
        now = dt.datetime.now(tz=dt.UTC)
        for uri in ("test://recoverable", "test://reduced"):
            await _mark_absent(db, kb_id=kb_id, uri=uri, when=now)
            await tombstone_document(
                db,
                tenant_id=tenant,
                kb_id=kb_id,
                data_source_id=ds_id,
                source_uri=uri,
                reason="source_absent",
                reduce_now=False,
                now=now,
            )
        await reduce_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds_id,
            source_uri="test://reduced",
            reason="source_absent",
            deleted_before=now + dt.timedelta(seconds=1),
            now=now,
        )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            ok = await c.post(
                f"/api/v1/knowledge/bases/{kb_id}/documents/restore",
                params={"sourceUri": "test://recoverable", "dataSourceId": str(ds_id)},
                headers=_headers(tenant),
            )
            gone = await c.post(
                f"/api/v1/knowledge/bases/{kb_id}/documents/restore",
                params={"sourceUri": "test://reduced", "dataSourceId": str(ds_id)},
                headers=_headers(tenant),
            )
    assert ok.status_code == 200, ok.text
    assert gone.status_code == 404, gone.text

    async with app_session(tenant) as db:
        back = await _live(db, kb_id, "test://recoverable")
        assert back
        assert all(c.missing_since is None for c in back)
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        ctx, _ = await retrieve_kb_context(
            db, agent=reloaded, tenant_id=tenant, query_text="board meeting minutes"
        )
        assert "recoverable minutes" in ctx
        assert "reduced minutes" not in ctx


# ------------------------------------------------------------------ counters


async def test_freshness_is_recomputed_not_decremented(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delta on a counter that was never right cannot converge.

    `run_source_sync` touches `kb.freshness` nowhere today, so every
    connector-fed KB card is already wrong -- and a decrementing counter would
    clamp at 0 and then count up from 0, which is a new kind of wrong on the
    number the freshness dashboard reads.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    docs = [_raw(f"test://doc-{i}", f"body number {i}") for i in range(4)]
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        # A counter that already disagrees with the table, which is the state
        # every connector-fed base is in today.
        kb.freshness = {"docs": 1, "chunks": 1}
        await db.flush()

        monkeypatch.setitem(registry._CONNECTORS, STUB, _StubConnector(docs))
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        await db.refresh(kb, ["freshness"])
        assert kb.freshness["docs"] == 4, "the sync maintains freshness at all now"
        assert kb.freshness["chunks"] == len(
            (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars().all()
        )

        now = dt.datetime.now(tz=dt.UTC)
        for doc in docs:
            await tombstone_document(
                db,
                tenant_id=tenant,
                kb_id=kb.id,
                data_source_id=ds.id,
                source_uri=doc.source_uri,
                reason="operator_delete",
                reduce_now=True,
                now=now,
            )
        await recompute_freshness(db, tenant_id=tenant, kb_id=kb.id)
        await db.refresh(kb, ["freshness"])
        assert kb.freshness["docs"] == 0
        assert kb.freshness["chunks"] == 0
        assert kb.freshness["docs"] >= 0 and kb.freshness["chunks"] >= 0
        await db.refresh(ds, ["doc_count"])
        assert ds.doc_count == 0


# ------------------------------------------------------------------ every generation


async def test_an_operator_delete_erases_the_superseded_generation_too(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance test's own question, asked of a document that was EDITED.

    Measured before the per-document delete stopped reading through
    `live_chunks()`: ingest -> re-ingest -> DELETE left the erased sentence
    sitting in `kb_chunk.content` under `deleted_reason = 'superseded'` with
    `reduced_at IS NULL`. Since amendment A3 made superseding unconditional that
    is every re-ingested document in the product, and the only thing that would
    ever have emptied it is the sweep -- off by default. The receipt lied too:
    `chunks`, `chars` and `sha256` described the live generation alone.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    sentence = "the reorganisation of the Braunschweig depot begins in March"
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        await ingest_raw_document(
            db, tenant_id=tenant, data_source=ds, kb_id=kb_id, raw=_raw("test://memo", sentence)
        )
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb_id,
            raw=_raw("test://memo", "an unrelated later revision"),
        )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.request(
                "DELETE",
                f"/api/v1/knowledge/bases/{kb_id}/documents",
                params={"sourceUri": "test://memo", "dataSourceId": str(ds_id)},
                headers=_headers(tenant),
            )
    assert r.status_code == 200, r.text
    receipt = r.json()

    async with app_session(tenant) as db:
        readable = (
            await db.execute(
                text("SELECT count(*) FROM kb_chunk WHERE content LIKE :pat"),
                {"pat": f"%{sentence}%"},
            )
        ).scalar_one()
        assert readable == 0, "the erased sentence is still readable in kb_chunk"

        rows = await _all_chunks(db, kb_id, "test://memo")
        assert len(rows) == 2, "both generations are still there as tombstones"
        assert all(c.reduced_at is not None and c.content == "" for c in rows)
        assert {c.deleted_reason for c in rows} == {"operator_delete"}, (
            "a generation destroyed by an operator is labelled as one, whatever "
            "took it out of retrieval first"
        )

        events = await _events(db, tenant)
        assert [e.action for e in events] == [DOCUMENT_DELETED]
        assert events[0].resource["chunks"] == receipt["chunks"] == 2, (
            "the receipt describes what was destroyed, not just the live generation"
        )
        assert events[0].resource["chars"] == len(sentence) + len("an unrelated later revision")
        assert await verify_chain(db, tenant) is True


async def test_restore_refuses_an_operator_deleted_document(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7 rule 2: a machine's guess is reversible, a human's command is not.

    Restore used to select `deleted_at IS NOT NULL AND reduced_at IS NULL` with
    no reason at all and lean on "un-reduced therefore inferred". That was
    measured false -- the superseded generation of an operator-deleted document
    is exactly an un-reduced tombstone, and this route answered 200 and put the
    erased text back into retrieval. The reason is a predicate now, so the
    refusal holds even if some path leaves an unreduced operator tombstone.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    sentence = "the reorganisation of the Braunschweig depot begins in March"
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        await ingest_raw_document(
            db, tenant_id=tenant, data_source=ds, kb_id=kb_id, raw=_raw("test://memo", sentence)
        )
        # An unreduced operator tombstone, built directly: this is the state the
        # restore predicate must refuse on its own terms, without depending on
        # the erasure path having reduced every generation it touched.
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb_id,
            data_source_id=ds_id,
            source_uri="test://memo",
            reason="operator_delete",
            reduce_now=False,
            now=dt.datetime.now(tz=dt.UTC),
        )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            back = await c.post(
                f"/api/v1/knowledge/bases/{kb_id}/documents/restore",
                params={"sourceUri": "test://memo", "dataSourceId": str(ds_id)},
                headers=_headers(tenant),
            )
    assert back.status_code == 404, back.text
    async with app_session(tenant) as db:
        assert await _live(db, kb_id, "test://memo") == [], "the erased document came back"


async def test_deleting_one_source_still_lets_another_sync_the_same_uri(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppression is scoped to the source whose rows an operator erased.

    Two sources can emit the same `source_uri` into one base -- two Drive folders
    holding the same file id -- and `tombstone_source` stamps `operator_delete`
    on one source's rows. Keyed on the URI alone, deleting source A permanently
    suppressed that URI for source B: B's copy was skipped on every later sync
    and could never be updated again, with only `stats["suppressed"]` to show for
    it. The existing `test_delete_source_leaves_the_other_sources_documents`
    misses this because it never syncs B again.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    shared = "test://shared-file-id"
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        a = await _source(db, tenant)
        b = await _source(db, tenant)
        for ds in (a, b):
            await ingest_raw_document(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb.id,
                raw=_raw(shared, f"the copy {ds.id} holds"),
            )

        await tombstone_source(db, tenant_id=tenant, data_source=a, now=dt.datetime.now(tz=dt.UTC))
        assert await suppressed_uris(
            db, tenant_id=tenant, kb_id=kb.id, data_source_id=a.id
        ) == frozenset({shared})
        assert (
            await suppressed_uris(db, tenant_id=tenant, kb_id=kb.id, data_source_id=b.id)
            == frozenset()
        ), "B's copy was never erased, so nothing about B is suppressed"

        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw(shared, "B's newer copy")])
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=b, kb_id=kb.id)
        assert job.stats["suppressed"] == 0
        assert [c.content for c in await _live(db, kb.id, shared)] == ["B's newer copy"]


async def test_unlink_source_from_base_erases_only_that_pairs_content(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse of `run_source_sync`'s "cluster several sources into a
    base": removing ONE source's content from ONE base, leaving the source
    itself (still usable elsewhere) and the base itself (still usable by
    other sources) both live. Narrower than `tombstone_source` (would also
    retire source A entirely, breaking its OTHER base) and `tombstone_base`
    (would also erase source B's content in this same base)."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        a = await _source(db, tenant)
        b = await _source(db, tenant)
        await ingest_raw_document(
            db, tenant_id=tenant, data_source=a, kb_id=kb.id, raw=_raw("test://a-doc", "A's text")
        )
        await ingest_raw_document(
            db, tenant_id=tenant, data_source=b, kb_id=kb.id, raw=_raw("test://b-doc", "B's text")
        )
        kb_id, a_id = kb.id, a.id

        removal = await unlink_source_from_base(
            db, tenant_id=tenant, kb=kb, data_source=a, now=dt.datetime.now(tz=dt.UTC)
        )
        assert removal.documents == 1
        assert removal.chunks == 1
        assert removal.sha256 == hashlib.sha256(b"test://a-doc").hexdigest()
        await flush_audit(db)  # the receipt exists once the chain is written
        assert removal.audit_seq is not None

        # A's content is gone from this base...
        assert await _live(db, kb_id, "test://a-doc") == []
        a_rows = await _all_chunks(db, kb_id, "test://a-doc")
        assert len(a_rows) == 1
        assert a_rows[0].reduced_at is not None
        assert a_rows[0].content == ""
        assert a_rows[0].deleted_reason == "operator_delete"

        # ...but B's is completely untouched.
        assert [c.content for c in await _live(db, kb_id, "test://b-doc")] == ["B's text"]

        # The source and the base are both still live -- unlike
        # tombstone_source/tombstone_base, this touches neither row's own
        # deleted_at.
        source_a = await db.get(m.DataSource, a_id)
        assert source_a is not None and source_a.deleted_at is None and source_a.connected is True
        base = await db.get(m.KnowledgeBase, kb_id)
        assert base is not None and base.deleted_at is None

        # Freshness reflects only B's now-sole live document.
        assert base.freshness is not None and base.freshness.get("docs") == 1

        events = await _events(db, tenant)
        assert [e.action for e in events] == [SOURCE_UNLINKED_FROM_BASE]
        assert events[0].resource["kb_id"] == str(kb_id)
        assert events[0].resource["data_source_id"] == str(a_id)
        assert await verify_chain(db, tenant) is True

        # Idempotent: nothing left of A's content in this base to reduce again.
        again = await unlink_source_from_base(
            db, tenant_id=tenant, kb=kb, data_source=a, now=dt.datetime.now(tz=dt.UTC)
        )
        assert again == Removal(documents=0, chunks=0, sha256=None, sources=0)


async def test_unlink_source_from_base_does_not_disturb_the_same_source_in_another_base(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source legitimately feeds several bases (the many-to-many
    relationship this slice's own module docstring describes) -- unlinking
    it from ONE base must not touch its content in another."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb1 = await _kb(db, tenant, "KB1")
        kb2 = await _kb(db, tenant, "KB2")
        source = await _source(db, tenant)
        await ingest_raw_document(
            db, tenant_id=tenant, data_source=source, kb_id=kb1.id, raw=_raw("test://x", "in kb1")
        )
        await ingest_raw_document(
            db, tenant_id=tenant, data_source=source, kb_id=kb2.id, raw=_raw("test://x", "in kb2")
        )
        kb1_id, kb2_id = kb1.id, kb2.id

        await unlink_source_from_base(
            db, tenant_id=tenant, kb=kb1, data_source=source, now=dt.datetime.now(tz=dt.UTC)
        )

        assert await _live(db, kb1_id, "test://x") == []
        assert [c.content for c in await _live(db, kb2_id, "test://x")] == ["in kb2"]


async def test_a_legacy_tombstone_suppresses_every_source(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same predicate, and the reason it is not a bare
    `data_source_id = :ds`.

    A pre-0045 chunk carries no provenance, so an operator erasing it leaves a
    tombstone whose owner nobody can name. Narrowing suppression to a source
    would let whichever source actually owns that URI ingest it straight back in
    on the next sync.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        db.add(
            m.KbChunk(
                tenant_id=tenant,
                kb_id=kb.id,
                content="a legacy copy nobody can attribute",
                embedding=None,
                source_uri="test://legacy-erased",
                chunk_metadata={"chunk_index": 0},
            )
        )
        await db.flush()
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=None,
            source_uri="test://legacy-erased",
            reason="operator_delete",
            reduce_now=True,
            now=dt.datetime.now(tz=dt.UTC),
        )

        assert await suppressed_uris(
            db, tenant_id=tenant, kb_id=kb.id, data_source_id=ds.id
        ) == frozenset({"test://legacy-erased"})
        monkeypatch.setitem(
            registry._CONNECTORS,
            STUB,
            _StubConnector([_raw("test://legacy-erased", "walked straight back in")]),
        )
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert job.stats["suppressed"] == 1
        assert await _live(db, kb.id, "test://legacy-erased") == []


# ------------------------------------------------------------------ what a delete locks


async def test_a_sync_recounts_its_own_source_and_no_other(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`recompute_freshness` used to UPDATE `doc_count` for every source feeding
    the base.

    In `run_source_sync` that lands right after `_advance_cursor` has locked this
    source's row, so two concurrent syncs into one base each hold their own row
    and then reach for the other's inside one statement -- AB/BA, and Postgres
    aborts one of them. It aborts OUTSIDE the try that sets `fatal`, so the
    exception escapes `run_source_sync`, the worker's `tenant_session` rolls
    back, and every document that sync ingested is lost with the job left
    un-transitioned. Row locks are not observable from one session, so what is
    pinned here is their cause: which rows the statement writes at all.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        quiet = await _source(db, tenant)
        syncing = await _source(db, tenant)
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=quiet,
            kb_id=kb.id,
            raw=_raw("test://quiet", "a document the other source owns"),
        )
        # A counter that disagrees with the table, so "left alone" and
        # "recomputed to the same number" cannot be confused.
        quiet.doc_count = 99
        await db.flush()

        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://busy", "the synced document")])
        )
        await run_source_sync(db, tenant_id=tenant, data_source=syncing, kb_id=kb.id)

        await db.refresh(quiet, ["doc_count"])
        await db.refresh(syncing, ["doc_count"])
        assert quiet.doc_count == 99, "a sync of one source must not write another source's row"
        assert syncing.doc_count == 1
        await db.refresh(kb, ["freshness"])
        assert kb.freshness["docs"] == 2, "the base itself is still recounted in full"


async def test_the_ledger_entry_is_appended_after_the_erasure_and_the_counters(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`append_event` takes `pg_advisory_xact_lock(hashtextextended(tenant))` and
    holds it until commit.

    Appending first meant one `DELETE /knowledge/sources/{id}` held that lock
    across a scan over `kb_chunk` and an UPDATE of every source feeding the base,
    stalling every other audited action in the tenant -- every tool call, every
    approval, every authz decision. §5.2 forbids exactly this hazard for a
    ledger entry per superseded document. Appending last costs nothing, so the
    order is pinned by observing the database from inside the append.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    import oc8.knowledge.tombstone as tombstone_mod

    real_append = tombstone_mod.append_event
    seen: list[tuple[str, int, int]] = []

    async def _observe(db: Any, **kwargs: Any) -> Any:
        left = (
            await db.execute(
                text(
                    "SELECT count(*) FROM kb_chunk WHERE kb_id = :kb "
                    "AND (content <> '' OR deleted_at IS NULL)"
                ),
                {"kb": kwargs["resource"].get("kb_id")},
            )
        ).scalar_one()
        counted = (
            await db.execute(
                text("SELECT (freshness->>'docs')::int FROM knowledge_base WHERE id = :kb"),
                {"kb": kwargs["resource"].get("kb_id")},
            )
        ).scalar_one()
        seen.append((kwargs["action"], int(left), -1 if counted is None else int(counted)))
        return await real_append(db, **kwargs)

    monkeypatch.setattr(tombstone_mod, "append_event", _observe)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=ds,
            kb_id=kb.id,
            raw=_raw("test://ordered", "text that must already be gone"),
        )
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://ordered",
            reason="operator_delete",
            reduce_now=True,
            now=dt.datetime.now(tz=dt.UTC),
        )

    assert seen == [(DOCUMENT_DELETED, 0, 0)], (
        "by the time the tenant's audit lock is taken, the content is destroyed "
        "and the counters are recomputed -- the lock is held for the append alone"
    )


async def test_every_operator_path_appends_after_its_own_expensive_work(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same audit-lock rule as the test above, at the four other call sites.

    Measured: with only `tombstone_document` pinned, hoisting the append back
    above the destructive work in `restore_document`, `tombstone_source` and
    `tombstone_base` left the entire suite green -- three of the five places the
    fix was applied had no test that would notice it being undone. That is the
    only reason this test exists, so it observes the same thing the other one
    does (the database, from inside the append) at every remaining site.

    What each number discriminates: `docs` is written by `recompute_freshness`,
    which is the scan the tenant's advisory lock must not be held across;
    `grants` is the DELETE in `tombstone_base`; `live` is the un-tombstoning
    UPDATE in `restore_document`.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    import oc8.knowledge.tombstone as tombstone_mod

    real_append = tombstone_mod.append_event
    seen: list[tuple[str, int, int, int, int]] = []
    watched: uuid.UUID | None = None

    async def _observe(db: Any, **kwargs: Any) -> Any:
        assert watched is not None
        readable, live, grants, docs = (
            await db.execute(
                text(
                    "SELECT (SELECT count(*) FROM kb_chunk WHERE kb_id = :kb "
                    "         AND content <> ''),"
                    "       (SELECT count(*) FROM kb_chunk WHERE kb_id = :kb "
                    "         AND deleted_at IS NULL),"
                    "       (SELECT count(*) FROM knowledge_grant WHERE kb_id = :kb),"
                    "       coalesce((SELECT (freshness->>'docs')::int "
                    "         FROM knowledge_base WHERE id = :kb), -1)"
                ),
                {"kb": watched},
            )
        ).one()
        seen.append((kwargs["action"], int(readable), int(live), int(grants), int(docs)))
        return await real_append(db, **kwargs)

    tenant = uuid.uuid4()
    now = dt.datetime.now(tz=dt.UTC)
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        watched = kb.id
        a = await _source(db, tenant)
        b = await _source(db, tenant)
        await _granted_agent(db, tenant, kb.id)
        for ds, uri in ((a, "test://alpha"), (b, "test://beta")):
            await ingest_raw_document(
                db, tenant_id=tenant, data_source=ds, kb_id=kb.id, raw=_raw(uri, f"text of {uri}")
            )
        await recompute_freshness(db, tenant_id=tenant, kb_id=kb.id, source_ids={a.id, b.id})
        # An INFERRED tombstone, so there is something for restore to undo.
        await _mark_absent(db, kb_id=kb.id, uri="test://beta", when=now)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=b.id,
            source_uri="test://beta",
            reason="source_absent",
            reduce_now=False,
            now=now,
        )

        monkeypatch.setattr(tombstone_mod, "append_event", _observe)
        await restore_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=b.id,
            source_uri="test://beta",
            now=now,
        )
        await tombstone_source(db, tenant_id=tenant, data_source=a, now=now)
        await tombstone_base(db, tenant_id=tenant, kb=kb, now=now)

    assert seen == [
        # Beta is back in retrieval and the base has been recounted to 2 already.
        (DOCUMENT_RESTORED, 2, 2, 1, 2),
        # Alpha's text is destroyed and the recount to 1 has landed.
        (SOURCE_DELETED, 1, 1, 1, 1),
        # Everything is destroyed, the grant is gone and the recount to 0 landed.
        (BASE_DELETED, 0, 0, 0, 0),
    ], seen


# ------------------------------------------------------ restore undoes a guess


async def test_restore_refuses_a_document_that_is_already_live(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore undoes a GUESS. It does not reinstate a version the customer
    replaced.

    Amendment A3 made superseding unconditional, so every re-ingested document in
    the product carries a restorable `superseded` generation until the sweep --
    off by default -- reduces it. Measured through the real `retrieve_kb_context`:
    one 200 on this route put BOTH the old and the new price into the agent's
    context, which is verbatim the defect `_supersede_previous_generation` exists
    to remove.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        agent = await _granted_agent(db, tenant, kb.id)
        for body in ("the price is ONE HUNDRED euros", "the price is TWO HUNDRED euros"):
            monkeypatch.setitem(
                registry._CONNECTORS, STUB, _StubConnector([_raw("test://memo", body)])
            )
            await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        brought_back = await restore_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://memo",
            now=dt.datetime.now(tz=dt.UTC),
        )
        assert brought_back == 0

        context, _ = await retrieve_kb_context(
            db, tenant_id=tenant, agent=agent, query_text="price"
        )
        assert "TWO HUNDRED" in context
        assert "ONE HUNDRED" not in context, context

        # And the listing does not invite the click either: the group has a live
        # chunk in it, so it is a live document with its live chunk count and no
        # deletion at all. It used to report the CURRENT document as
        # `chunks=2 deleted_reason='superseded'`, which is how an operator was
        # led here.
        docs = await list_documents(db, tenant_id=tenant, kb_id=kb.id, include_deleted=True)
        listed = [(d.source_uri, d.chunks, d.deleted_at, d.deleted_reason) for d in docs]
        assert listed == [("test://memo", 1, None, None)], listed


async def test_restore_brings_back_only_the_generation_that_died_last(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document edited twice and then removed upstream has three unreduced
    generations. Bringing all of them back would put three versions of one
    document into the same base -- the same defect as restoring a superseded
    generation beside a live one, only larger."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        for body in ("policy version ONE", "policy version TWO", "policy version THREE"):
            await ingest_raw_document(
                db,
                tenant_id=tenant,
                data_source=ds,
                kb_id=kb.id,
                raw=_raw("test://policy", body),
            )
        now = dt.datetime.now(tz=dt.UTC)
        await _mark_absent(db, kb_id=kb.id, uri="test://policy", when=now)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://policy",
            reason="source_absent",
            reduce_now=False,
            now=now,
        )

        brought_back = await restore_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://policy",
            now=now,
        )
        assert brought_back == 1
        live = await _live(db, kb.id, "test://policy")
        assert [c.content for c in live] == ["policy version THREE"], [c.content for c in live]


async def test_restore_reaches_one_source_while_another_still_answers(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dataSourceId` omitted means every source's copy of that URI, and one
    source having replaced its copy says nothing about another's -- so the
    refusal is per source, not per request."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        answering = await _source(db, tenant)
        vanished = await _source(db, tenant)
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=answering,
            kb_id=kb.id,
            raw=_raw("test://shared", "the copy that is still there"),
        )
        await ingest_raw_document(
            db,
            tenant_id=tenant,
            data_source=vanished,
            kb_id=kb.id,
            raw=_raw("test://shared", "the copy that went away"),
        )
        now = dt.datetime.now(tz=dt.UTC)
        await db.execute(
            update(m.KbChunk)
            .where(
                m.KbChunk.source_uri == "test://shared",
                m.KbChunk.data_source_id == vanished.id,
            )
            .values(missing_since=now)
        )
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=vanished.id,
            source_uri="test://shared",
            reason="source_absent",
            reduce_now=False,
            now=now,
        )

        brought_back = await restore_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=None,
            source_uri="test://shared",
            now=now,
        )
        assert brought_back == 1
        live = await _live(db, kb.id, "test://shared")
        assert sorted(c.content for c in live) == [
            "the copy that is still there",
            "the copy that went away",
        ]
