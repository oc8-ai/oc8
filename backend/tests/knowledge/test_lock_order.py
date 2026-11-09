"""One lock order, and what it costs when a lock is lost anyway.

Every path in this slice that removes knowledge touches the same three lock
objects -- the `data_source` row, the `knowledge_base` row and the tenant's audit
advisory lock -- and it was measured taking them in three different orders:

    SYNC                : ['data_source', 'knowledge_base', 'data_source']
    OPERATOR DOC DELETE : ['knowledge_base', 'data_source', 'ADVISORY', ...]
    HELD SYNC           : ['ADVISORY', 'data_source', ...]

Two AB/BA cycles, both reproduced against real rows with "deadlock detected",
and the first of them reachable on a default deployment: deleting ONE document
while a source syncs into that base needs no flag at all.

So this file does two things. It traces the real statements the real functions
emit and fails the build if any path inverts the order written down in
`tombstone.py`'s module docstring -- because an order that only holds by luck is
an order that the next edit silently breaks. And it pins the consequence: a
transaction lost to a deadlock in the post-fetch bookkeeping used to take the
whole sync's ingested documents with it, which is a wholly disproportionate
price for failing to update a counter.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import event, select, text, update

from oc8 import models as m
from oc8.audit.chain import flush_audit
from oc8.config import Settings, get_settings
from oc8.knowledge.connectors import registry
from oc8.knowledge.connectors.base import Attestation, SourceListing
from oc8.knowledge.ingest import run_source_sync
from oc8.knowledge.reconcile import observe_listing
from oc8.knowledge.tombstone import (
    OPERATOR_DELETE,
    SOURCE_ABSENT,
    reduce_document,
    restore_document,
    tombstone_base,
    tombstone_document,
    tombstone_source,
    unlink_source_from_base,
)
from tests.conftest import AppSessionFactory
from tests.knowledge.test_tombstone import (
    STUB,
    _FakeEmbedRouter,
    _kb,
    _mark_absent,
    _org,
    _raw,
    _source,
    _StubConnector,
)

pytestmark = pytest.mark.asyncio

_WRITE = re.compile(r"^\s*(UPDATE|INSERT INTO|DELETE FROM)\s+(\w+)", re.IGNORECASE)

#: The order of `tombstone.py`'s module docstring, as a number per lock object.
#: `kb_chunk` is not here: it is always first (every path reads and writes the
#: chunks before it touches anything else) and its rows are per-document, so
#: ordering it against itself is a different question from this one.
_RANK = {"data_source": 0, "knowledge_base": 1, "ADVISORY": 2, "audit_event": 3}


def _trace(db: Any, out: list[str]) -> Callable[[], None]:
    """Record the first time each lock object is taken, in order.

    A write to a row IS the lock acquisition -- Postgres has no separate step --
    and `pg_advisory_xact_lock` is the audit chain's serialisation. Repeats are
    collapsed because only the FIRST acquisition can deadlock: the second time a
    transaction touches a row it already holds it.
    """
    engine = db.get_bind().engine

    def before(conn: Any, cur: Any, statement: str, *args: Any) -> None:
        if "pg_advisory_xact_lock" in statement:
            name = "ADVISORY"
        else:
            hit = _WRITE.match(statement)
            if hit is None or hit.group(2).lower() not in _RANK:
                return
            name = hit.group(2).lower()
        if name not in out:
            out.append(name)

    event.listen(engine, "before_cursor_execute", before)
    return lambda: event.remove(engine, "before_cursor_execute", before)


def _assert_ordered(label: str, order: list[str]) -> None:
    ranks = [_RANK[name] for name in order]
    assert ranks == sorted(ranks), (
        f"{label} takes its locks as {order}, which is not the one order "
        f"{sorted(order, key=_RANK.__getitem__)}; a path that inverts two of "
        "these deadlocks against every path that does not"
    )


@pytest.fixture
def reconcile_on(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = get_settings()
    monkeypatch.setattr(settings, "knowledge_reconcile_enabled", True, raising=False)
    return settings


class _Attesting(_StubConnector):
    def __init__(self, docs: list[Any], listing: SourceListing) -> None:
        super().__init__(docs)
        self._listing = listing

    async def attest_listing(self, config: Any, auth: Any = None) -> SourceListing:
        return self._listing


async def test_every_path_takes_data_source_then_knowledge_base_then_the_audit_lock(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_on: Settings
) -> None:
    """Eight paths, one order. Six of the original seven had it wrong."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    now = dt.datetime.now(tz=dt.UTC)
    orders: dict[str, list[str]] = {}

    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://a", "the a document")])
        )
        orders["sync"] = []
        stop = _trace(db, orders["sync"])
        try:
            await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()

    # An operator erasing ONE document -- the surface that needs no flag at all.
    async with app_session(tenant) as db:
        kb2 = await _kb(db, tenant, "KB2")
        ds2 = await _source(db, tenant)
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://b", "the b document")])
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds2, kb_id=kb2.id)
        orders["operator document delete"] = []
        stop = _trace(db, orders["operator document delete"])
        try:
            await tombstone_document(
                db,
                tenant_id=tenant,
                kb_id=kb2.id,
                data_source_id=ds2.id,
                source_uri="test://b",
                reason=OPERATOR_DELETE,
                reduce_now=True,
                now=now,
            )
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()

    # The sweep's own phase 1, then phase 2, then a restore.
    async with app_session(tenant) as db:
        kb3 = await _kb(db, tenant, "KB3")
        ds3 = await _source(db, tenant)
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://c", "the c document")])
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds3, kb_id=kb3.id)
        await _mark_absent(db, kb_id=kb3.id, uri="test://c", when=now)

        orders["sweep tombstone"] = []
        stop = _trace(db, orders["sweep tombstone"])
        try:
            await tombstone_document(
                db,
                tenant_id=tenant,
                kb_id=kb3.id,
                data_source_id=ds3.id,
                source_uri="test://c",
                reason=SOURCE_ABSENT,
                reduce_now=False,
                now=now,
            )
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()

        orders["restore"] = []
        stop = _trace(db, orders["restore"])
        try:
            restored = await restore_document(
                db,
                tenant_id=tenant,
                kb_id=kb3.id,
                data_source_id=ds3.id,
                source_uri="test://c",
                now=now,
            )
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()
        assert restored == 1

        await _mark_absent(db, kb_id=kb3.id, uri="test://c", when=now)
        await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb3.id,
            data_source_id=ds3.id,
            source_uri="test://c",
            reason=SOURCE_ABSENT,
            reduce_now=False,
            now=now,
        )
        orders["sweep reduce"] = []
        stop = _trace(db, orders["sweep reduce"])
        try:
            await reduce_document(
                db,
                tenant_id=tenant,
                kb_id=kb3.id,
                data_source_id=ds3.id,
                source_uri="test://c",
                reason=SOURCE_ABSENT,
                deleted_before=now + dt.timedelta(seconds=1),
                now=now,
            )
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()

    # A sync whose attested listing is REFUSED: the hold appends a chain row, and
    # it used to do so BEFORE the freshness recount wrote `knowledge_base`.
    async with app_session(tenant) as db:
        kb4 = await _kb(db, tenant, "KB4")
        ds4 = await _source(db, tenant)
        good = _Attesting(
            [_raw("test://d", "the d document")],
            SourceListing(
                attestation=Attestation.AUTHORITATIVE, present_uris=frozenset({"test://d"})
            ),
        )
        monkeypatch.setitem(registry._CONNECTORS, STUB, good)
        await run_source_sync(db, tenant_id=tenant, data_source=ds4, kb_id=kb4.id)
        kb4_id, ds4_id = kb4.id, ds4.id

    async with app_session(tenant) as db:
        live = await db.get(m.DataSource, ds4_id)
        assert live is not None
        # The credential died: an empty AUTHORITATIVE listing is refused.
        blind = _Attesting(
            [], SourceListing(attestation=Attestation.AUTHORITATIVE, present_uris=frozenset())
        )
        monkeypatch.setitem(registry._CONNECTORS, STUB, blind)
        orders["held sync"] = []
        stop = _trace(db, orders["held sync"])
        try:
            job = await run_source_sync(db, tenant_id=tenant, data_source=live, kb_id=kb4_id)
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()
        assert (job.stats or {})["reconcile"] == "held_empty", job.stats

    async with app_session(tenant) as db:
        source = await db.get(m.DataSource, ds4_id)
        assert source is not None
        orders["operator source delete"] = []
        stop = _trace(db, orders["operator source delete"])
        try:
            await tombstone_source(db, tenant_id=tenant, data_source=source, now=now)
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()

    # A base that still has a LIVE source feeding it, so the recount really does
    # write `data_source` as well as `knowledge_base` -- otherwise "the orders
    # agree" would only mean "this path took one of the two locks".
    async with app_session(tenant) as db:
        kb5 = await _kb(db, tenant, "KB5")
        ds5 = await _source(db, tenant)
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://e", "the e document")])
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds5, kb_id=kb5.id)
        orders["operator base delete"] = []
        stop = _trace(db, orders["operator base delete"])
        try:
            await tombstone_base(db, tenant_id=tenant, kb=kb5, now=now)
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()
        assert orders["operator base delete"][:2] == ["data_source", "knowledge_base"], orders[
            "operator base delete"
        ]

    # An operator removing ONE source from ONE base -- unlink_source_from_base,
    # the reverse of run_source_sync's "cluster several sources into a base"
    # relationship. Composed from the same _reduce_scope/recompute_freshness/
    # append_event primitives as tombstone_source/tombstone_base, so it must
    # take the same order.
    async with app_session(tenant) as db:
        kb7 = await _kb(db, tenant, "KB7")
        ds7 = await _source(db, tenant)
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://g", "the g document")])
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds7, kb_id=kb7.id)
        orders["operator unlink source from base"] = []
        stop = _trace(db, orders["operator unlink source from base"])
        try:
            await unlink_source_from_base(db, tenant_id=tenant, kb=kb7, data_source=ds7, now=now)
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()
        assert orders["operator unlink source from base"][:2] == [
            "data_source",
            "knowledge_base",
        ], orders["operator unlink source from base"]

    # The observation on its own. It runs inside a sync today, behind the cursor
    # write-back that has already taken the `data_source` row -- so the rule "the
    # audit append is last" has to be true of the FUNCTION, not only of the one
    # caller that happens to have locked the row first.
    async with app_session(tenant) as db:
        kb6 = await _kb(db, tenant, "KB6")
        ds6 = await _source(db, tenant)
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://f", "the f document")])
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds6, kb_id=kb6.id)
        kb6_id, ds6_id = kb6.id, ds6.id

    async with app_session(tenant) as db:
        source = await db.get(m.DataSource, ds6_id)
        assert source is not None
        orders["refused observation"] = []
        stop = _trace(db, orders["refused observation"])
        try:
            outcome = await observe_listing(
                db,
                tenant_id=tenant,
                data_source=source,
                kb_id=kb6_id,
                connector=_Attesting(
                    [],
                    SourceListing(attestation=Attestation.AUTHORITATIVE, present_uris=frozenset()),
                ),
                auth=None,
                fatal=None,
                now=now,
            )
            # The chain is written at commit; do it here so the trace sees it.
            await flush_audit(db)
        finally:
            stop()
        assert outcome == "held_empty"
        assert orders["refused observation"][0] == "data_source", orders["refused observation"]

    for label, order in orders.items():
        _assert_ordered(label, order)
    # Not vacuous: the two paths the reproduction paired must really have taken
    # both row locks, or "they agree" would only mean "neither did anything".
    assert orders["sync"][:2] == ["data_source", "knowledge_base"], orders["sync"]
    assert orders["operator document delete"][:2] == ["data_source", "knowledge_base"], orders[
        "operator document delete"
    ]
    assert orders["held sync"].index("ADVISORY") > orders["held sync"].index("knowledge_base")


async def test_a_sync_whose_bookkeeping_dies_still_keeps_the_documents_it_ingested(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadlock should cost the sync's bookkeeping, not its work.

    The abort a deadlock delivers lands in the post-fetch block, which sat
    OUTSIDE the try that catches connector failures: the exception escaped
    `run_source_sync`, the worker's `tenant_session` rolled the whole thing back,
    and every document the sync had already ingested and flushed was lost with
    the job left un-transitioned -- so the next sync's hash cursor was the only
    record that anything had happened.

    A division by zero stands in for the deadlock on purpose: it is the same
    thing to Postgres, an error that poisons the whole transaction, so a Python
    `try` around the call is NOT enough and only the savepoint saves the work.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()

    async def exploding(db: Any, **kwargs: Any) -> None:
        await db.execute(text("SELECT 1 / 0"))

    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        monkeypatch.setitem(
            registry._CONNECTORS,
            STUB,
            _StubConnector([_raw("test://survivor", "the sentence that must survive")]),
        )
        monkeypatch.setattr("oc8.knowledge.ingest.recompute_freshness", exploding)
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        # It did not escape, and the job is not left "running".
        assert job.status == "partial", job.stats
        assert any("bookkeeping failed" in e for e in (job.stats or {}).get("errors", []))
        assert (job.stats or {})["ingested"] == 1

    # The outer transaction committed, so the documents are really there.
    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.KbChunk.content).where(
                        m.KbChunk.kb_id == kb_id, m.KbChunk.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == ["the sentence that must survive"], rows
        # And the bookkeeping the savepoint rolled back really did not happen --
        # otherwise this test would pass on an implementation that never failed.
        source = (
            await db.execute(select(m.DataSource).where(m.DataSource.id == ds_id))
        ).scalar_one()
        assert source.last_sync_at is None
        assert source.cursor.get("hashes") in (None, [])


async def test_a_reconciliation_that_cannot_write_costs_only_the_reconciliation(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, reconcile_on: Settings
) -> None:
    """The observation half of the same rule. It runs after the cursor write-back
    now, so a failure there must not undo the cursor either."""
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()

    async def exploding(db: Any, **kwargs: Any) -> str:
        await db.execute(text("SELECT 1 / 0"))
        return "unreachable"

    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        kb_id, ds_id = kb.id, ds.id
        monkeypatch.setitem(
            registry._CONNECTORS,
            STUB,
            _StubConnector([_raw("test://kept", "the sentence that must survive")]),
        )
        monkeypatch.setattr("oc8.knowledge.ingest.observe_listing", exploding)
        job = await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)
        assert job.status == "partial", job.stats
        assert (job.stats or {})["reconcile"] == "error"

    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.KbChunk.content).where(
                        m.KbChunk.kb_id == kb_id, m.KbChunk.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == ["the sentence that must survive"], rows
        source = (
            await db.execute(select(m.DataSource).where(m.DataSource.id == ds_id))
        ).scalar_one()
        # The cursor write-back is in its own savepoint and precedes the
        # observation, so it survives the observation's failure.
        assert source.last_sync_at is not None
        assert source.cursor["hashes"], source.cursor


async def test_the_mark_the_sweep_kills_on_is_re_read_where_it_writes(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absence may only remove what an observation MARKED.

    `tombstone_document(reason='source_absent')` used to re-select whatever was
    live, which is how a fresh generation with `missing_since IS NULL` -- the row
    carrying its own proof it was never missed -- was tombstoned as absent.
    """
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _org(db, tenant)
        kb = await _kb(db, tenant)
        ds = await _source(db, tenant)
        monkeypatch.setitem(
            registry._CONNECTORS, STUB, _StubConnector([_raw("test://never", "never missing")])
        )
        await run_source_sync(db, tenant_id=tenant, data_source=ds, kb_id=kb.id)

        removal = await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://never",
            reason=SOURCE_ABSENT,
            reduce_now=False,
            now=dt.datetime.now(tz=dt.UTC),
        )
        assert removal is None
        live = (
            (
                await db.execute(
                    select(m.KbChunk.deleted_at).where(m.KbChunk.source_uri == "test://never")
                )
            )
            .scalars()
            .all()
        )
        assert live == [None]

        # And an operator's own DELETE is untouched by that predicate: a human
        # named the object, and no observation has to agree with them.
        await db.execute(
            update(m.KbChunk)
            .where(m.KbChunk.source_uri == "test://never")
            .values(missing_since=None)
        )
        erased = await tombstone_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            data_source_id=ds.id,
            source_uri="test://never",
            reason=OPERATOR_DELETE,
            reduce_now=True,
            now=dt.datetime.now(tz=dt.UTC),
        )
        assert erased is not None and erased.chunks == 1
