"""Taking a document back out of a knowledge base, in two phases.

Until this module existed a customer could put a document into a knowledge base
and never take it out: there was no DELETE anywhere on the knowledge API, so the
only removal path in the product was raw SQL against `kb_chunk`.

**Phase 1, tombstone.** `deleted_at` is set and the content and the embedding are
RETAINED. The chunk is out of retrieval and restorable by one UPDATE, so nothing
has been destroyed and nothing is written to the ledger.

**Phase 2, reduce.** The content is destroyed and the digest of what was
destroyed goes into the tenant's hash chain -- §12.5.1's rule that retention is
REDUCTION: the row survives as its own tombstone, only the ability to re-read it
is dropped.

Which phase runs when is rule 2 of the design: **a human's command is executed, a
machine's inference is held.** An operator DELETE reduces immediately and
irreversibly (that is the GDPR path, and a human named the object). A deletion
the system inferred -- from an attested listing, or from a re-ingest -- is
tombstoned now and reduced by the sweep only after a grace window, during which
`restore_document` brings it back.

**THE LOCK ORDER, and it is the same one everywhere in this slice:**

    kb_chunk  ->  data_source  ->  knowledge_base  ->  the tenant audit lock

Every path that removes knowledge takes these in that order, and each object only
counts the FIRST time it is acquired. `recompute_freshness` is where the middle
two are ordered (`data_source` before `knowledge_base`, deliberately, because
that is the order a sync already had), and the audit chain is written at commit
(`oc8.audit.chain`), which flushes before it takes `pg_advisory_xact_lock` so the
audit lock cannot arrive before a caller's row work.
`tests/knowledge/test_lock_order.py` traces the real statements of all six paths
and fails the build if one inverts.

It is written down because it was measured: `recompute_freshness` used to write
`knowledge_base` first and `data_source` second, which gave every operator path
and the sweep's own phase 1 the OPPOSITE order to a live sync, and Postgres
aborted the pair ("deadlock detected") on real rows with every flag off. The
abort is not symmetric either -- on the sync side it lands in bookkeeping that
runs after the documents are already ingested, so the cost of getting this wrong
was a whole sync's work, not a retried statement.

Two things that look like details and are not:

* `ck_kb_chunk_reduced_is_empty` means the three assignments that reduce a row --
  `content = ''`, `embedding = NULL`, `reduced_at` -- must travel in ONE
  statement together with `deleted_at`, or the statement raises. That is
  deliberate: a reduced row that still holds retrievable text is unrepresentable
  in the table, so "prove the document is gone" is a `count(*)` the customer can
  run rather than an audit of every path that might have forgotten to blank it.
* Every function here is flush-only and the caller owns the commit. Tenant
  isolation is a transaction-local Postgres GUC, so a commit inside one of these
  would unbind RLS and every later statement would silently see nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import ColumnElement, delete, distinct, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import append_event
from oc8.auth import Principal
from oc8.knowledge.chunks import live_chunks

CATEGORY = "knowledge"

DOCUMENT_DELETED = "knowledge.document_deleted"  # operator, per document, content destroyed
SOURCE_DELETED = "knowledge.source_deleted"  # operator, one aggregate event
BASE_DELETED = "knowledge.base_deleted"  # operator, one aggregate event
SOURCE_UNLINKED_FROM_BASE = "knowledge.source_unlinked_from_base"  # operator, one aggregate event
DOCUMENT_REDUCED = "knowledge.document_reduced"  # sweep, per document, source_absent
SUPERSEDED_REDUCED = "knowledge.superseded_reduced"  # sweep, one aggregate event per tick
DOCUMENT_RESTORED = "knowledge.document_restored"
RECONCILE_HELD = "knowledge.reconcile_held"

#: The three legal values of `kb_chunk.deleted_reason`. Not cosmetic: the reason
#: decides reduction timing (immediate or after the grace window), write-path
#: suppression (§5.3 -- only an operator's deletion stays deleted) and cursor
#: eviction (§5.6 -- only an absence is forgotten, so the document can come back
#: if it is restored upstream).
OPERATOR_DELETE = "operator_delete"
SOURCE_ABSENT = "source_absent"
SUPERSEDED = "superseded"


@dataclass
class Removal:
    """What one removal actually touched, so the caller can report a blast radius
    instead of hiding it.

    `sources` and `audit_seq` are here rather than re-derived by the API layer:
    `api/v1/knowledge.py` is deliberately NOT on the read-path allowlist, so it
    cannot count `kb_chunk` rows for itself, and `audit_seq` is the receipt that
    turns "prove it" into a lookup rather than a support ticket.
    """

    documents: int
    chunks: int
    sha256: str | None
    sources: int = 0
    # The ledger entry itself, not its seq: `append_event` writes the chain when
    # the transaction COMMITS, so the seq does not exist yet when a removal
    # returns. Every caller that reports the receipt commits first.
    audit_event: m.AuditEvent | None = None

    @property
    def audit_seq(self) -> int | None:
        if self.audit_event is None:
            return None
        seq = cast("int | None", self.audit_event.seq)
        if seq is None:
            raise RuntimeError("Removal.audit_seq is only known once the transaction has committed")
        return seq


def _digest(parts: Iterable[str]) -> str:
    """The digest of what is about to become unreadable, taken BEFORE it is."""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _chunk_order(chunk: m.KbChunk) -> tuple[int, str]:
    """Chunks are hashed in the order they were split, so the digest of a
    document is reproducible from the same bytes. `chunk_metadata` is bare JSONB
    written by several paths, so a missing or non-numeric index sorts first
    rather than raising in the middle of a deletion.

    The id breaks the tie because an operator erasure now spans every unreduced
    generation of a document and generations reuse the same `chunk_index` from
    zero: without it the digest over the same bytes would depend on the order
    the database happened to return the rows in.
    """
    try:
        index = int((chunk.chunk_metadata or {}).get("chunk_index", 0))
    except (TypeError, ValueError):
        index = 0
    return (index, str(chunk.id))


def _document_scope(
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    data_source_id: uuid.UUID | None,
    source_uri: str,
) -> list[ColumnElement[bool]]:
    """Document identity: (tenant_id, data_source_id, kb_id, source_uri).

    `data_source_id=None` is NARROWING-off, not a wildcard bug: it means "every
    source's copy of that URI in that base, including pre-0045 rows whose
    provenance the migration honestly refused to guess". That is the only way an
    operator reaches legacy connector chunks, and it is what "erase this
    document, wherever it landed" has to mean.
    """
    where: list[ColumnElement[bool]] = [
        m.KbChunk.tenant_id == tenant_id,
        m.KbChunk.kb_id == kb_id,
        m.KbChunk.source_uri == source_uri,
    ]
    if data_source_id is not None:
        where.append(m.KbChunk.data_source_id == data_source_id)
    return where


# --------------------------------------------------------------- phase 1


async def tombstone_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    data_source_id: uuid.UUID | None,
    source_uri: str,
    reason: str,
    reduce_now: bool,
    now: dt.datetime,
    principal: Principal | None = None,
) -> Removal | None:
    """Take one document out of retrieval. Returns None if it was not there.

    `reduce_now` is the operator path and nothing else sets it: the content is
    destroyed in this same transaction and one `knowledge.document_deleted`
    entry names the URI, the chunk count and the digest of what it destroyed. An
    inferred deletion (`source_absent`, `superseded`) keeps its content for the
    grace window, so no entry is written -- nothing has been destroyed yet, and
    a claim in an append-only ledger cannot be taken back if the inference turns
    out to be wrong (`audit_event` has UPDATE and DELETE revoked from `oc8_app`).

    The two paths read DIFFERENT work sets, and that is the whole of the fix
    below: an operator erases every generation of the document that still holds
    text, an inference replaces only the generation that is answerable now.
    """
    scope = _document_scope(
        tenant_id=tenant_id, kb_id=kb_id, data_source_id=data_source_id, source_uri=source_uri
    )
    if reduce_now:
        # Measured before this predicate existed: ingest -> re-ingest -> DELETE
        # left `SELECT count(*) FROM kb_chunk WHERE content LIKE '%<sentence>%'`
        # returning 1. `live_chunks()` here reached only the CURRENT generation,
        # and since amendment A3 made supersede unconditional every re-ingested
        # document carries a previous generation with `deleted_reason =
        # 'superseded'`, `reduced_at IS NULL` and its full content retained. The
        # only thing that would ever have emptied it is the sweep, which is off
        # by default -- so the GDPR path left the erased text in the table for
        # ever. This is the predicate `_reduce_scope` already applies to the
        # source and base deletes, for the reason it states there: an erasure
        # that leaves an already-superseded generation's text readable in the
        # same base would not be an erasure.
        work = select(m.KbChunk).where(*scope, m.KbChunk.reduced_at.is_(None))
    elif reason == SOURCE_ABSENT:
        # An absence may only remove what an observation actually MARKED. The
        # sweep reads its work list in one transaction and tombstones each
        # document in a later one, and without this predicate the second
        # transaction re-selected whatever was live by then. Measured: a sync
        # landing in that window re-ingested the document, so the marked
        # generation became `superseded` and a fresh, unmarked generation went
        # live -- and the sweep tombstoned THAT one, as `source_absent`, seven
        # days before writing an unrepairable ledger entry saying it had been
        # absent from every attested listing. The row carried its own proof it
        # was never marked: `missing_since IS NULL`.
        work = live_chunks().where(*scope, m.KbChunk.missing_since.is_not(None))
    else:
        work = live_chunks().where(*scope)
    chunks = list((await db.execute(work)).scalars().all())
    if not chunks:
        return None

    chunks.sort(key=_chunk_order)
    ids = [c.id for c in chunks]
    sources = len({c.data_source_id for c in chunks})
    values: dict[str, Any] = {
        "deleted_at": now,
        "deleted_reason": reason,
        # A document that is gone is not "missing" any more; leaving the mark
        # would let the sweep's two-facts rule fire again on a row it already
        # killed.
        "missing_since": None,
    }
    sha256: str | None = None
    chars = 0
    audit_event: m.AuditEvent | None = None

    if reduce_now:
        # Over everything the UPDATE below is about to empty, not just the live
        # generation: a receipt that under-reports what it destroyed is the same
        # lie as a deletion that did not happen. Both are read off HERE and not
        # in the ledger entry below, which is now written after the destruction:
        # the UPDATE synchronises the session, so by then every `c.content` is
        # the empty string and `chars` would be reported as 0.
        sha256 = _digest(c.content for c in chunks)
        chars = sum(len(c.content) for c in chunks)
        values.update(
            {
                # COALESCE so a generation already tombstoned by an inference
                # keeps the timestamp of when it actually left retrieval, exactly
                # as `_reduce_scope` does for the source and base paths.
                "deleted_at": func.coalesce(m.KbChunk.deleted_at, now),
                "content": "",
                "embedding": None,
                "reduced_at": now,
            }
        )

    await db.execute(update(m.KbChunk).where(m.KbChunk.id.in_(ids)).values(**values))

    if reason != SUPERSEDED:
        # Superseding runs once per changed document inside a sync, and
        # `run_source_sync` recomputes once at the end. Recomputing here as well
        # would put one aggregate over the widest table in the schema between
        # every pair of documents.
        await recompute_freshness(
            db,
            tenant_id=tenant_id,
            kb_id=kb_id,
            source_ids={c.data_source_id for c in chunks if c.data_source_id is not None},
        )

    if reduce_now:
        # The ledger entry goes LAST, after the destruction and after the
        # counters, and it is still in this one transaction -- do NOT insert a
        # commit before it, which would leave a durable claim with the tenant GUC
        # gone. (The tenant's audit lock is taken when the transaction commits,
        # not here -- see `oc8.audit.chain` -- so the position no longer decides
        # how long that lock is held; the receipt's seq is read after commit.)
        event = await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="operator",
            actor_id=None,
            category=CATEGORY,
            action=DOCUMENT_DELETED,
            resource={
                "kb_id": str(kb_id),
                "data_source_id": str(data_source_id) if data_source_id else None,
                "source_uri": source_uri,
                "sources": sources,
                "chunks": len(chunks),
                "chars": chars,
                "sha256": sha256,
            },
            decision="allow",
            reason="operator deleted the document",
            principal=principal,
        )
        audit_event = event

    await db.flush()
    return Removal(
        documents=1, chunks=len(chunks), sha256=sha256, sources=sources, audit_event=audit_event
    )


async def restore_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    data_source_id: uuid.UUID | None,
    source_uri: str,
    now: dt.datetime,
    principal: Principal | None = None,
) -> int:
    """Undo a deletion the system INFERRED. Returns the number of chunks brought
    back.

    Rule 2 of §1 is a predicate here, not an implication drawn from
    `reduced_at IS NULL`. That implication -- "un-reduced therefore the system
    guessed" -- was measured false: an operator delete that spanned a superseded
    generation left an unreduced `operator_delete` row, and this route answered
    200 and put the erased text back into retrieval. Naming the two inferred
    reasons keeps restore safe even if some future path leaves an unreduced
    operator tombstone behind again.

    Two more predicates say what "undo a guess" excludes, and both were measured
    as data loss of the other kind -- stale text back into retrieval:

    * **A document that is LIVE is not restored.** Amendment A3 made superseding
      unconditional, so every re-ingested document in the product carries a
      restorable `superseded` generation until the sweep (off by default) reduces
      it. Restoring it put both versions into the agent's context from one 200 --
      verbatim the defect `_supersede_previous_generation` exists to remove.
    * **Only the LATEST tombstoned generation comes back.** A document with two
      superseded predecessors and a `source_absent` current generation would
      otherwise resurrect three versions of itself at once. Chunks of one
      generation are tombstoned by a single UPDATE and so share one `deleted_at`,
      which is what makes "the latest one" a group rather than a guess.

    Both are per source: `data_source_id=None` reaches every source's copy of the
    URI, and one source having replaced its copy says nothing about another's.
    """
    scope = _document_scope(
        tenant_id=tenant_id, kb_id=kb_id, data_source_id=data_source_id, source_uri=source_uri
    )
    rows = (
        await db.execute(
            select(m.KbChunk.id, m.KbChunk.data_source_id, m.KbChunk.deleted_at).where(
                *scope,
                m.KbChunk.deleted_at.is_not(None),
                m.KbChunk.reduced_at.is_(None),
                m.KbChunk.deleted_reason.in_([SOURCE_ABSENT, SUPERSEDED]),
            )
        )
    ).all()
    if not rows:
        return 0

    answering = set(
        (
            await db.execute(
                select(distinct(m.KbChunk.data_source_id)).where(
                    *scope, m.KbChunk.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    latest: dict[uuid.UUID | None, dt.datetime] = {}
    for _, ds_id, deleted_at in rows:
        if ds_id in answering or deleted_at is None:
            continue
        seen = latest.get(ds_id)
        if seen is None or deleted_at > seen:
            latest[ds_id] = deleted_at
    ids = [chunk_id for chunk_id, ds_id, deleted_at in rows if latest.get(ds_id) == deleted_at]
    if not ids:
        # Every tombstone this URI has belongs to a source that is currently
        # answering with it. There is no guess left to undo, and the route says
        # so with the same 404 as a URI that never existed.
        return 0

    await db.execute(
        update(m.KbChunk)
        .where(m.KbChunk.id.in_(ids))
        # `ck_kb_chunk_deleted_reason` binds these two together: clearing one
        # without the other is refused by the table.
        .values(deleted_at=None, deleted_reason=None, missing_since=None)
    )
    await recompute_freshness(
        db,
        tenant_id=tenant_id,
        kb_id=kb_id,
        source_ids={ds_id for ds_id in latest if ds_id is not None},
    )
    # Appended last, in this same transaction; the chain is written (and the
    # tenant's audit lock taken) when it commits.
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="operator",
        actor_id=None,
        category=CATEGORY,
        action=DOCUMENT_RESTORED,
        resource={
            "kb_id": str(kb_id),
            "data_source_id": str(data_source_id) if data_source_id else None,
            "source_uri": source_uri,
            "chunks": len(ids),
        },
        decision="allow",
        principal=principal,
    )
    await db.flush()
    return len(ids)


# --------------------------------------------------------------- phase 2


async def reduce_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    data_source_id: uuid.UUID | None,
    source_uri: str,
    reason: str,
    deleted_before: dt.datetime,
    now: dt.datetime,
) -> Removal | None:
    """Destroy the content of ONE tombstoned generation. Returns None if there
    was nothing left to destroy.

    `reason` and `deleted_before` are the caller's description of which
    generation it means, and they are re-applied here rather than trusted: this
    function used to re-select every unreduced chunk of the document, which the
    sweep's work list never asks for, and three things were measured to follow.
    A generation still inside its own grace window was destroyed early and could
    no longer be restored. `reason` was read off whichever row the database
    returned first, so a `source_absent` document's content could be destroyed
    with no `knowledge.document_reduced` entry, no digest and no cursor eviction
    -- or a ledger sha256 could silently cover superseded content as well. And
    the second work-list row for the same document then found nothing and was
    miscounted as "another worker got there first".

    `deleted_before` is the sweep's own `now - knowledge_reduce_after_hours`, so
    the work list and the destruction cannot disagree about the grace window.

    A `superseded` generation is reduced without an entry of its own: the sweep
    writes ONE aggregate `knowledge.superseded_reduced` per tenant per tick.
    Per-document rows for routine re-syncing would grow an append-only table
    without bound as a side effect of ordinary operation, and each append takes
    the tenant's audit lock.
    """
    scope = _document_scope(
        tenant_id=tenant_id, kb_id=kb_id, data_source_id=data_source_id, source_uri=source_uri
    )
    generation = [
        *scope,
        m.KbChunk.deleted_at.is_not(None),
        m.KbChunk.reduced_at.is_(None),
        m.KbChunk.deleted_reason == reason,
        m.KbChunk.deleted_at < deleted_before,
    ]
    chunks = list((await db.execute(select(m.KbChunk).where(*generation))).scalars().all())
    if not chunks:
        return None

    chunks.sort(key=_chunk_order)
    since = min(c.deleted_at for c in chunks if c.deleted_at is not None)
    sha256 = _digest(c.content for c in chunks)
    chars = sum(len(c.content) for c in chunks)
    sources = len({c.data_source_id for c in chunks})

    source = None
    if data_source_id is not None:
        source = await db.get(m.DataSource, data_source_id)

    result = await db.execute(
        update(m.KbChunk)
        # The full predicate again, not just the ids: `ck_kb_chunk_reduced_is_empty`
        # needs `deleted_at IS NOT NULL` to hold at the moment `reduced_at` lands,
        # and a concurrent restore -- which clears `deleted_at` and
        # `deleted_reason` together -- is the one thing that could have changed
        # any of it since the SELECT above under READ COMMITTED.
        .where(m.KbChunk.id.in_([c.id for c in chunks]), *generation)
        # The three together, or the constraint refuses the row.
        .values(content="", embedding=None, reduced_at=now)
    )
    destroyed = getattr(result, "rowcount", 0) or 0
    if destroyed == 0:
        # THIS is the claim re-read, and it has to be the write rather than the
        # read: appending first left a permanent, unrepairable entry claiming a
        # destruction that a restore landing in between had just prevented, and
        # `audit_event` has UPDATE and DELETE revoked from `oc8_app`. Returning
        # None also keeps the sweep from counting it. §5.1's rule, applied to
        # phase 2: write nothing when zero rows match.
        return None
    if destroyed != len(chunks):
        # Unreachable through any path in this repo -- restore moves a whole
        # document at once -- but a digest that names bytes still sitting in the
        # table is exactly what the ledger must never contain, so fail the unit
        # and let the sweep retry it rather than write one.
        raise RuntimeError(
            f"reduce_document destroyed {destroyed} of {len(chunks)} chunks of "
            f"{source_uri!r}; the digest would not describe what was destroyed"
        )

    if reason == SOURCE_ABSENT and source is not None:
        await _evict_from_cursor(db, source=source, source_uri=source_uri)

    audit_event: m.AuditEvent | None = None
    if reason != SUPERSEDED:
        # Last, and inside this same transaction: the entry and the destruction
        # commit together (the chain itself is written at commit).
        event = await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            category=CATEGORY,
            action=DOCUMENT_REDUCED,
            resource={
                "kb_id": str(kb_id),
                "data_source_id": str(data_source_id) if data_source_id else None,
                "source_uri": source_uri,
                "chunks": len(chunks),
                "chars": chars,
                # The hash outlives the bytes it names.
                "sha256": sha256,
                "connector_type": source.connector_type if source is not None else None,
            },
            decision="allow",
            reason=f"absent from every attested listing of this source since {since}",
        )
        audit_event = event

    # No `recompute_freshness` here on purpose: reduction changes no live row --
    # every chunk it touches was already tombstoned -- so the counts it would
    # recompute are exactly the ones the tombstone already wrote.
    await db.flush()
    return Removal(
        documents=1, chunks=len(chunks), sha256=sha256, sources=sources, audit_event=audit_event
    )


async def _evict_from_cursor(db: AsyncSession, *, source: m.DataSource, source_uri: str) -> None:
    """Forget a reduced document's digest, so it can come back if it is restored
    upstream.

    Three connectors skip a document whose content digest is already in
    `cursor["hashes"]`, so without this the document would be skipped for ever.
    The digest is dropped ONLY when no other URI still maps to it: two documents
    with identical bytes share one digest, and `hashes` holds bare digests, so
    evicting on the first reduction would make the second document re-ingest on
    every sync for the rest of time.

    Durability of an ERASURE does not depend on any of this -- write-path
    suppression (§5.3) is what makes an operator's deletion stick, and it works
    for a connector that ignores the cursor entirely.
    """
    locked = (
        await db.execute(
            select(m.DataSource)
            .where(m.DataSource.tenant_id == source.tenant_id, m.DataSource.id == source.id)
            # SKIP LOCKED rather than waiting: if the source is mid-sync, that
            # sync is about to rewrite the whole cursor dict from its own copy,
            # so blocking would only queue up a lost update. The document stays
            # skipped until the next re-ingest of that source, which costs a
            # document that could have come back and never costs content.
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        return

    cursor = dict(locked.cursor or {})
    uri_hashes = dict(cursor.get("uri_hashes") or {})
    digest = uri_hashes.pop(source_uri, None)
    if digest is None:
        # Written before this slice, or by a path that never recorded the map.
        # Nothing to subtract, and guessing which digest belonged to this URI is
        # exactly the kind of guess `data_source_id` exists to prevent.
        return

    cursor["uri_hashes"] = uri_hashes
    if digest not in uri_hashes.values():
        cursor["hashes"] = [h for h in cursor.get("hashes", []) if h != digest]
    # The column is bare JSONB with no MutableDict, so in-place mutation would
    # silently not persist; every writer reassigns the whole dict.
    locked.cursor = cursor
    await db.flush()


# --------------------------------------------------------------- operator scopes


@dataclass
class _Scope:
    """What one set-based erasure hit, read off before it hit it."""

    uris: list[str]
    kb_ids: set[uuid.UUID]
    #: Only the sources that can be named. A pre-0045 row's provenance is NULL
    #: and there is nothing to recount for it -- but it is still a distinct
    #: "source" in the blast radius the caller reports, which is why `sources`
    #: is counted separately rather than derived from this.
    source_ids: set[uuid.UUID]
    sources: int
    chunks: int


async def _reduce_scope(
    db: AsyncSession, *, scope: Sequence[ColumnElement[bool]], now: dt.datetime
) -> _Scope:
    """Destroy everything in one scope with ONE statement, and say what it hit.

    Deliberately not a loop over `reduce_document`: hashing the CONTENT of a
    5000-document corpus would materialise hundreds of MB of strings inside one
    HTTP request, and one ledger entry per document would be N sequential chain
    appends each holding the tenant's audit lock. The caller digests the sorted
    URI list instead -- per-document proof is not lost, because every tombstone
    row still names its own `source_uri` with its own `deleted_at`.

    The predicate is `reduced_at IS NULL` rather than `deleted_at IS NULL`: an
    erasure that left an already-superseded generation's text readable in the
    same base would not be an erasure.
    """
    rows = (
        await db.execute(
            select(m.KbChunk.kb_id, m.KbChunk.source_uri, m.KbChunk.data_source_id)
            .where(*scope, m.KbChunk.reduced_at.is_(None))
            .distinct()
        )
    ).all()
    result = await db.execute(
        update(m.KbChunk)
        .where(*scope, m.KbChunk.reduced_at.is_(None))
        .values(
            # COALESCE so a document already tombstoned by an inference keeps the
            # timestamp of when it actually left retrieval.
            deleted_at=func.coalesce(m.KbChunk.deleted_at, now),
            deleted_reason=OPERATOR_DELETE,
            reduced_at=now,
            content="",
            embedding=None,
            missing_since=None,
        )
        # The scope can be a whole corpus, and no caller here reads the rows back
        # through this session, so the RETURNING round trip that keeps in-session
        # objects in step would be paid for nothing.
        .execution_options(synchronize_session=False)
    )
    # CursorResult carries rowcount; the base Result type does not, and the
    # execute() overload for an UPDATE is not narrow enough to say so.
    chunks = getattr(result, "rowcount", 0) or 0
    return _Scope(
        uris=sorted({uri for _, uri, _ in rows}),
        kb_ids={kb_id for kb_id, _, _ in rows},
        source_ids={ds_id for _, _, ds_id in rows if ds_id is not None},
        sources=len({ds_id for _, _, ds_id in rows}),
        chunks=chunks,
    )


async def tombstone_source(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    now: dt.datetime,
    principal: Principal | None = None,
) -> Removal:
    """Erase everything one source ever ingested, and retire the source.

    Retired, not vaporised: the `IngestionJob` rows are the operational record of
    what it did, and every tombstone it leaves behind points back at it.
    """
    hit = await _reduce_scope(
        db,
        scope=[m.KbChunk.tenant_id == tenant_id, m.KbChunk.data_source_id == data_source.id],
        now=now,
    )
    sha256 = _digest(hit.uris) if hit.uris else None

    data_source.deleted_at = now
    # A retired source must stop being a live connection as well as a live
    # corpus, or the next scheduled sync walks straight back in.
    data_source.connected = False
    data_source.schedule_cron = None
    for kb_id in hit.kb_ids:
        # Only this source's counter: no other source's rows were touched, so no
        # other source's `doc_count` changed.
        await recompute_freshness(db, tenant_id=tenant_id, kb_id=kb_id, source_ids={data_source.id})
    data_source.doc_count = 0
    # Appended after the erasure and the counters, in the same transaction.
    # The chain is written (and the tenant's audit lock taken) at commit.
    event = await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="operator",
        actor_id=None,
        category=CATEGORY,
        action=SOURCE_DELETED,
        resource={
            "data_source_id": str(data_source.id),
            "connector_type": data_source.connector_type,
            "documents": len(hit.uris),
            "chunks": hit.chunks,
            # Over the sorted distinct URI list, not over content: see
            # `_reduce_scope`.
            "sha256": sha256,
        },
        decision="allow",
        reason="operator deleted the source",
        principal=principal,
    )
    await db.flush()
    return Removal(
        documents=len(hit.uris), chunks=hit.chunks, sha256=sha256, sources=1, audit_event=event
    )


async def tombstone_base(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb: m.KnowledgeBase,
    now: dt.datetime,
    principal: Principal | None = None,
) -> Removal:
    """Erase a whole knowledge base, whichever sources fed it.

    Its grants are hard-DELETEd rather than tombstoned: a grant is an access
    rule, not a record, and `_grants_for` reads every grant unconditionally, so a
    dangling one on a deleted base is an authz hazard rather than history worth
    keeping.
    """
    hit = await _reduce_scope(
        db,
        scope=[m.KbChunk.tenant_id == tenant_id, m.KbChunk.kb_id == kb.id],
        now=now,
    )
    sha256 = _digest(hit.uris) if hit.uris else None

    await db.execute(
        delete(m.KnowledgeGrant).where(
            m.KnowledgeGrant.tenant_id == tenant_id, m.KnowledgeGrant.kb_id == kb.id
        )
    )
    # Every source that fed this base really did lose documents, so this is the
    # one caller that legitimately recounts more than one -- and it is an
    # operator erasing a whole base, not something that runs on every sync.
    await recompute_freshness(db, tenant_id=tenant_id, kb_id=kb.id, source_ids=hit.source_ids)
    # AFTER the recount, not before it. Assigned first, this pending ORM change
    # went out on `recompute_freshness`'s first statement -- an UPDATE
    # knowledge_base ahead of the UPDATE data_source, which is the module
    # docstring's lock order backwards and the measured deadlock against a live
    # sync. The recount does not read `deleted_at`, so nothing else moves.
    kb.deleted_at = now
    # Appended last, for the audit-lock reason in `tombstone_source`.
    event = await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="operator",
        actor_id=None,
        category=CATEGORY,
        action=BASE_DELETED,
        resource={
            "kb_id": str(kb.id),
            "name": kb.name,
            "documents": len(hit.uris),
            "chunks": hit.chunks,
            "sha256": sha256,
        },
        decision="allow",
        reason="operator deleted the knowledge base",
        principal=principal,
    )
    await db.flush()
    return Removal(
        documents=len(hit.uris),
        chunks=hit.chunks,
        sha256=sha256,
        sources=hit.sources,
        audit_event=event,
    )


async def unlink_source_from_base(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb: m.KnowledgeBase,
    data_source: m.DataSource,
    now: dt.datetime,
    principal: Principal | None = None,
) -> Removal:
    """Erase one source's content from one base, leaving both the source and
    the base otherwise untouched -- narrower than `tombstone_source` (retires
    the WHOLE source, every base it ever fed) and `tombstone_base` (erases
    the WHOLE base, every source that ever fed it). The many-to-many
    "cluster several sources into a base" relationship this slice's own
    module docstring describes has, until now, had no reverse: `run_source_sync`
    can ADD a source's content to a base, but nothing could take it back out
    short of retiring the source or the base entirely.

    An operator naming a specific (source, base) pair is a human's command,
    so per rule 2 this reduces immediately and irreversibly, exactly like its
    two siblings above -- not tombstoned-with-a-grace-window.

    Unlike those two siblings, though, calling this on a pair with nothing
    left to remove (already unlinked, or never linked at all) is a real,
    reachable case -- not just a defensive branch: the API route this backs
    lets an operator retry or double-click. `reduce_document`'s §5.1 rule
    applies here the same way: write nothing (no freshness recompute, no
    audit entry) when zero rows match, or the ledger would carry a claim
    describing a removal that did not happen.
    """
    hit = await _reduce_scope(
        db,
        scope=[
            m.KbChunk.tenant_id == tenant_id,
            m.KbChunk.kb_id == kb.id,
            m.KbChunk.data_source_id == data_source.id,
        ],
        now=now,
    )
    if hit.chunks == 0:
        return Removal(documents=0, chunks=0, sha256=None, sources=0)

    sha256 = _digest(hit.uris) if hit.uris else None

    # Only this (source, base) pair's counter: no other source's rows in this
    # base, and no other base this source feeds, were touched.
    await recompute_freshness(db, tenant_id=tenant_id, kb_id=kb.id, source_ids={data_source.id})

    # Appended after the erasure and the counters, like
    # tombstone_source/tombstone_base; the chain is written at commit.
    event = await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="operator",
        actor_id=None,
        category=CATEGORY,
        action=SOURCE_UNLINKED_FROM_BASE,
        resource={
            "kb_id": str(kb.id),
            "data_source_id": str(data_source.id),
            "documents": len(hit.uris),
            "chunks": hit.chunks,
            "sha256": sha256,
        },
        decision="allow",
        reason="operator removed the source from this knowledge base",
        principal=principal,
    )
    await db.flush()
    return Removal(
        documents=len(hit.uris),
        chunks=hit.chunks,
        sha256=sha256,
        sources=1,
        audit_event=event,
    )


# --------------------------------------------------------------- counters


async def recompute_freshness(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    source_ids: Collection[uuid.UUID] = (),
) -> None:
    """Recount what a base holds, and the `doc_count` of the sources the CALLER
    just changed. One query each, exact.

    Recomputed rather than incremented: deltas on a counter that was never right
    cannot converge, and `run_source_sync` touches `kb.freshness` NOWHERE today,
    so every connector-fed base's card is already wrong. A decrementing counter
    would clamp at 0 and then count up from 0, which is a new kind of wrong on a
    number the freshness dashboard and the staleness gauge both read.

    `source_ids` is named by the caller rather than derived from "every source
    feeding this base", and that is not a micro-optimisation. Deriving it made
    `run_source_sync` take a row lock on EVERY other source in the base right
    after `_advance_cursor` had locked its own -- textbook AB/BA between two
    concurrent syncs into one base. Postgres aborts one of them, and it aborts
    outside the `try` that sets `fatal`, so the exception escaped
    `run_source_sync`, the worker's `tenant_session` rolled back, and every
    document that sync had ingested was lost with the job left un-transitioned.
    It was also O(sources-in-base) writes on every sync, and the upload connector
    mints one DataSource per uploaded file.

    `data_source` is written BEFORE `knowledge_base`, and that ordering is the
    module docstring's lock order, not a coincidence of how the code reads. The
    other way round -- which is what shipped -- gave every operator path and the
    sweep's phase 1 the sequence `['knowledge_base', 'data_source']` while a live
    sync emits `['data_source', 'knowledge_base', 'data_source']`, and a real
    pair of transactions in those two orders was measured to deadlock on real
    rows. The count below is the first statement of the function and therefore
    the autoflush point where a caller's pending ORM changes go out, so a caller
    that must not write `knowledge_base` before `data_source` assigns
    `kb.deleted_at` AFTER calling this, not before.
    """
    counted = (
        await db.execute(
            select(func.count(), func.count(distinct(m.KbChunk.source_uri))).where(
                m.KbChunk.tenant_id == tenant_id,
                m.KbChunk.kb_id == kb_id,
                m.KbChunk.deleted_at.is_(None),
            )
        )
    ).one()
    chunks, docs = int(counted[0]), int(counted[1])

    if source_ids:
        # `doc_count` is a property of the SOURCE, not of this base, so it counts
        # that source's live documents wherever they landed. One correlated
        # statement rather than a query per source, and the ids are sorted so two
        # callers updating overlapping sets take their row locks in the same
        # order.
        per_source = (
            select(func.count(distinct(m.KbChunk.source_uri)))
            .where(
                m.KbChunk.tenant_id == m.DataSource.tenant_id,
                m.KbChunk.data_source_id == m.DataSource.id,
                m.KbChunk.deleted_at.is_(None),
            )
            .correlate(m.DataSource)
            .scalar_subquery()
        )
        await db.execute(
            update(m.DataSource)
            .where(
                m.DataSource.tenant_id == tenant_id,
                m.DataSource.id.in_(sorted(source_ids)),
            )
            .values(doc_count=per_source)
            # The value is a SQL expression, so there is nothing for the ORM to
            # evaluate in Python; callers that hold a DataSource read it back.
            .execution_options(synchronize_session=False)
        )

    kb = await db.get(m.KnowledgeBase, kb_id)
    if kb is not None:
        fresh = dict(kb.freshness or {})
        fresh.update(
            {"docs": docs, "chunks": chunks, "updated": dt.datetime.now(tz=dt.UTC).isoformat()}
        )
        kb.freshness = fresh
    await db.flush()


async def suppressed_uris(
    db: AsyncSession, *, tenant_id: uuid.UUID, kb_id: uuid.UUID, data_source_id: uuid.UUID
) -> frozenset[str]:
    """The URIs an operator erased from this base FOR THIS SOURCE, for the write
    path to skip.

    An erasure the next sync undoes is not an erasure. Reason-aware by
    construction: `source_absent` must NOT suppress, or a document restored
    upstream could never come back, and `superseded` must NOT, or a document
    could only ever be updated once.

    Source-aware too, and that is the fix for a measured trap: two sources can
    emit the SAME `source_uri` into one base (two Drive folders holding the same
    file id), and `tombstone_source` stamps `operator_delete` on one source's
    rows. Keyed on the URI alone, deleting source A permanently suppressed that
    URI for source B -- B's own copy was skipped on every sync from then on and
    could never be updated again, silently, with only `stats["suppressed"]` to
    show for it.

    A tombstone whose `data_source_id` is NULL still suppresses for everyone: it
    is exactly the row whose owner nobody can name (written before 0045), so
    narrowing it to a source would let whichever source actually owns that URI
    walk an operator's erasure straight back in.
    """
    rows = (
        await db.execute(
            select(distinct(m.KbChunk.source_uri)).where(
                m.KbChunk.tenant_id == tenant_id,
                m.KbChunk.kb_id == kb_id,
                m.KbChunk.deleted_at.is_not(None),
                m.KbChunk.deleted_reason == OPERATOR_DELETE,
                or_(
                    m.KbChunk.data_source_id == data_source_id,
                    m.KbChunk.data_source_id.is_(None),
                ),
            )
        )
    ).scalars()
    return frozenset(rows.all())
