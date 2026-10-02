# backend/src/oc8/knowledge/ingest.py
"""Document ingestion pipeline (§11.1, minimal real loop): extract -> chunk
-> embed -> store. Upload connector only (text/markdown/PDF via pypdf); no
OCR, no OAuth connectors, no async queue — ingestion runs synchronously
inside the upload request. See
docs/superpowers/specs/2026-07-16-knowledge-rag-design.md for what's
deliberately deferred."""

from __future__ import annotations

import base64
import hashlib
import io
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.knowledge.connectors.base import AuthContext, Connector, RawDocument
from oc8.knowledge.connectors.context import SourceAuthContext
from oc8.knowledge.connectors.registry import resolve_connector
from oc8.knowledge.reconcile import observe_listing
from oc8.knowledge.tombstone import (
    SUPERSEDED,
    recompute_freshness,
    suppressed_uris,
    tombstone_document,
)
from oc8.modelrouter import EmbeddingUnavailable, get_model_router
from oc8.models.knowledge import EMBED_DIM
from oc8.observability import record_ingestion_job

MAX_DOCUMENT_LENGTH = 200_000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100


class IngestionError(RuntimeError):
    pass


class KnowledgeBaseNotFoundError(IngestionError):
    pass


def chunk_text(
    text: str, *, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(stripped):
        end = min(start + chunk_size, len(stripped))
        chunks.append(stripped[start:end])
        if end == len(stripped):
            break
        start = end - overlap
    return chunks


class _HtmlTextExtractor(HTMLParser):
    """Dependency-free tag stripper: collect visible text, skip script/style."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(part.strip() for part in self._parts if part.strip())


def extract_text(*, content: str, content_type: str) -> str:
    if content_type in ("text/plain", "text/markdown"):
        return content.strip()
    if content_type == "text/html":
        parser = _HtmlTextExtractor()
        parser.feed(content)
        parser.close()
        return parser.get_text().strip()
    if content_type == "application/pdf":
        try:
            raw = base64.b64decode(content)
            reader = PdfReader(io.BytesIO(raw))
            pages = [page.extract_text() or "" for page in reader.pages]
        except (PdfReadError, ValueError) as exc:
            raise IngestionError(f"failed to parse PDF: {exc}") from exc
        return "\n\n".join(pages).strip()
    if content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        import docx

        try:
            raw = base64.b64decode(content)
            doc = docx.Document(io.BytesIO(raw))
            text = "\n".join(p.text for p in doc.paragraphs if p.text)
        except Exception as exc:  # python-docx raises assorted zip/xml errors on garbage input
            raise IngestionError(f"failed to parse DOCX: {exc}") from exc
        return text.strip()
    if content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        import openpyxl

        try:
            raw = base64.b64decode(content)
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
            lines = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        lines.append(" | ".join(cells))
        except Exception as exc:
            raise IngestionError(f"failed to parse XLSX: {exc}") from exc
        return "\n".join(lines).strip()
    if content_type == "text/csv":
        import csv as csv_module

        try:
            reader = csv_module.reader(io.StringIO(content))
            lines = [" | ".join(row) for row in reader if row]
        except csv_module.Error as exc:
            raise IngestionError(f"failed to parse CSV: {exc}") from exc
        return "\n".join(lines).strip()
    raise IngestionError(f"unsupported content_type: {content_type!r}")


async def _supersede_previous_generation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_id: uuid.UUID,
    source_uri: str,
) -> None:
    """Take the generation this re-ingest replaces out of retrieval.

    Unconditional, per amendment A3: there is no `knowledge_supersede_enabled`
    and none should be added. Until this existed, an edited document ingested as
    a second document and its previous version stayed retrievable beside it for
    ever, so the agent answered from both with no way to tell which was current.
    That is a live defect rather than a new capability, and a flag defaulting off
    would leave it in place. The tombstone retains content and embedding for the
    grace window, so the repo's default-off habit for destructive switches
    applies to the REDUCTION that follows, not to this.

    The adoption UPDATE first: a chunk written before 0045 carries no provenance,
    and narrowing the tombstone to this source would walk straight past it,
    leaving the legacy generation live beside the new one for ever. A re-ingest
    of the same URI into the same base by this source is the evidence that
    authorises the attribution.
    """
    await db.execute(
        update(m.KbChunk)
        .where(
            m.KbChunk.tenant_id == tenant_id,
            m.KbChunk.kb_id == kb_id,
            m.KbChunk.source_uri == source_uri,
            m.KbChunk.data_source_id.is_(None),
            m.KbChunk.deleted_at.is_(None),
        )
        .values(data_source_id=data_source.id)
    )
    await tombstone_document(
        db,
        tenant_id=tenant_id,
        kb_id=kb_id,
        data_source_id=data_source.id,
        source_uri=source_uri,
        reason=SUPERSEDED,
        reduce_now=False,
        now=datetime.now(tz=UTC),
    )


async def ingest_raw_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_id: uuid.UUID,
    raw: RawDocument,
) -> int:
    """Connector-agnostic ingestion tail: extract -> chunk -> embed -> store.

    Returns the number of chunks written. add/flush only, never commits
    (tenant isolation is a transaction-local Postgres GUC, so a mid-service
    commit would drop the RLS binding). The caller owns the surrounding
    IngestionJob and freshness bookkeeping.

    `data_source` is finally load-bearing: every chunk records which source
    produced it. Without that column a chunk has no identity beyond its URI, and
    nothing about it can ever be reconciled against what its source still holds.

    Everything that can refuse this document -- an unsupported content type, an
    unparseable PDF, an embedding of the wrong width -- happens BEFORE the
    previous generation is touched. That ordering is the safety property: a
    document whose upstream version became unreadable must leave its old chunks
    exactly where they were.
    """
    extracted = extract_text(content=raw.content, content_type=raw.content_type)
    chunks = chunk_text(extracted)
    router = get_model_router()
    metadata = dict(raw.metadata)
    # The knowledge base's own choice at creation time, not the deployment-wide
    # default -- an operator who picked "local/bge-large" for its data-locality
    # promise must actually get bge-large, not whatever OC8_DEFAULT_EMBEDDING_MODEL
    # happens to be pointed at.
    kb = await db.get(m.KnowledgeBase, kb_id)
    embedding_model = (
        kb.embedding_model if kb is not None else get_settings().default_embedding_model
    )
    embeddings: list[list[float] | None] = []
    for chunk in chunks:
        try:
            embeddings.append(
                check_embedding_fits(
                    await router.embed(chunk, model=embedding_model), model=embedding_model
                )
            )
        except EmbeddingUnavailable:
            embeddings.append(None)

    if chunks:
        # Only when this re-ingest actually produced something. A document that
        # extracted to nothing is not a newer version of anything, and
        # superseding on it would silently empty the base.
        await _supersede_previous_generation(
            db,
            tenant_id=tenant_id,
            data_source=data_source,
            kb_id=kb_id,
            source_uri=raw.source_uri,
        )

    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
        db.add(
            m.KbChunk(
                tenant_id=tenant_id,
                data_source_id=data_source.id,
                kb_id=kb_id,
                content=chunk,
                embedding=embedding,
                source_uri=raw.source_uri,
                chunk_metadata={"chunk_index": i, **metadata},
            )
        )
    await db.flush()
    return len(chunks)


async def _advance_cursor(
    db: AsyncSession,
    *,
    data_source: m.DataSource,
    hashes: list[str],
    uri_hashes: dict[str, str],
) -> None:
    """Merge this sync's digests into the source cursor. Re-read, merge, reassign.

    The re-read replaces a blind overwrite from an in-memory copy that may be
    minutes old by the time the fetch loop ends. Two things now write this
    column: this function, and the sweep, which removes a reduced document's
    digest so the document can come back if it is restored upstream (§5.6).
    Overwriting from stale memory would undo that eviction, and it was already a
    lost-update bug between two concurrent syncs of one source.

    `uri_hashes` is MERGED and never replaced: an incremental sync only ever sees
    the documents that changed, so replacing the map would drop every URI this
    sync did not touch and leave the eviction with nothing to subtract.
    """
    await db.refresh(data_source, ["cursor"])
    cursor: dict[str, Any] = dict(data_source.cursor or {})
    cursor["hashes"] = sorted(set(cursor.get("hashes") or []) | set(hashes))
    cursor["uri_hashes"] = {**(cursor.get("uri_hashes") or {}), **uri_hashes}
    # Bare JSONB with no MutableDict: an in-place mutation would silently not
    # persist, so every writer reassigns the whole dict.
    data_source.cursor = cursor


async def run_source_sync(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    data_source: m.DataSource,
    kb_id: uuid.UUID,
    job: m.IngestionJob | None = None,
) -> m.IngestionJob:
    """Orchestrate a connector sync: loop the connector's fetch(), ingest each
    RawDocument, advance the source cursor, and record a single IngestionJob.

    When `job` is provided (the async worker passes the queued job it loaded),
    it is flipped to 'running' and reused; when None (direct/synchronous
    callers), a running job is created as before. Still flush-only, never
    commits -- the caller (endpoint or worker tenant_session) owns the commit.

    This is also the only code that knows whether a sync actually worked, which
    is why three things that look unrelated all live here: the write-path
    suppression that keeps an operator's erasure erased, the listing observation
    that stamps what a source no longer holds, and the rule that a connector-level
    failure advances nothing at all.
    """
    if job is None:
        job = m.IngestionJob(
            tenant_id=tenant_id,
            data_source_id=data_source.id,
            kb_id=kb_id,
            status="running",
        )
        db.add(job)
        await db.flush()
    else:
        job.status = "running"
        await db.flush()

    kb = await db.get(m.KnowledgeBase, kb_id)
    if kb is not None and (kb.index_type or "internal") != "internal":
        job.status = "failed"
        job.stats = {
            **(job.stats or {}),
            "error": (
                "this knowledge base is connected to an external vector index — "
                "sync is not supported"
            ),
        }
        await db.flush()
        return job

    stats: dict[str, Any] = {
        "fetched": 0,
        "ingested": 0,
        "skipped": 0,
        "suppressed": 0,
        "chunks": 0,
    }
    new_hashes: list[str] = []
    fetched_hashes: dict[str, str] = {}
    errors: list[str] = []
    fatal: str | None = None
    connector: Connector | None = None
    auth: AuthContext | None = None
    now = datetime.now(tz=UTC)

    try:
        connector = await resolve_connector(
            db, tenant_id=tenant_id, type_id=data_source.connector_type
        )
        # Always build a context: a connector may need a stored secret even
        # when the source has no OAuth connection (S3 keys, for instance).
        # token() is what refuses when there is no connected account.
        auth = SourceAuthContext(
            db, tenant_id=tenant_id, connection_id=data_source.oauth_connection_id
        )
        # Read once, before the first document. An erasure the next sync undoes
        # is not an erasure, and this is the only place that holds for a
        # connector which ignores the cursor entirely -- `upload.py` already
        # does, and any future etag/delta-token connector would too. A failure
        # here is deliberately fatal: ingesting without the suppression set would
        # walk an operator's deletion straight back in.
        suppressed = await suppressed_uris(
            db, tenant_id=tenant_id, kb_id=kb_id, data_source_id=data_source.id
        )
        async for raw in connector.fetch(data_source.config, data_source.cursor, auth):
            stats["fetched"] += 1
            if raw.source_uri in suppressed:
                stats["suppressed"] += 1
                continue
            try:
                n = await ingest_raw_document(
                    db, tenant_id=tenant_id, data_source=data_source, kb_id=kb_id, raw=raw
                )
            except IngestionError as exc:
                stats["skipped"] += 1
                errors.append(str(exc))
                continue
            stats["ingested"] += 1
            stats["chunks"] += n
            new_hashes.append(raw.content_hash)
            fetched_hashes[raw.source_uri] = raw.content_hash
    except Exception as exc:  # connector-level failure must not strand the job as "running"
        fatal = str(exc)

    # Everything from here down is BOOKKEEPING -- the cursor, the counters, the
    # listing observation -- and each half runs inside a SAVEPOINT of its own.
    #
    # Not defensive plumbing: measured. A deadlock on `data_source`/
    # `knowledge_base` (the lock order this slice now fixes, but any lock this
    # code takes can lose a race) aborts the whole transaction, and this block
    # sits OUTSIDE the try above, so the exception escaped `run_source_sync`,
    # the worker's `tenant_session` rolled back, and every document the sync had
    # already ingested and flushed was lost with the job left un-transitioned --
    # a wholly disproportionate price for failing to update a counter. Inside a
    # savepoint, the same abort rolls back to the savepoint and the documents,
    # which were flushed before it opened, survive to be committed. The job says
    # what did not get written.
    bookkeeping_error: str | None = None

    if fatal is None:
        # Amendment A2: a failed sync advances NOTHING. This bookkeeping used to
        # sit outside the try, so an expired token moved the cursor exactly as if
        # the sync had succeeded and every document the connector never reached
        # was then skipped by its own hash cursor for ever after. The job records
        # "failed" with its error -- that is the record of what happened -- and
        # the counters are recovered by the next successful sync, which counts
        # rows rather than trusting a counter.
        try:
            async with db.begin_nested():
                await _advance_cursor(
                    db, data_source=data_source, hashes=new_hashes, uri_hashes=fetched_hashes
                )
                data_source.last_sync_at = now.isoformat()
                # This source and no other. Recomputing every source feeding the
                # base took a row lock on each of them right after
                # `_advance_cursor` had locked this one, which is an AB/BA
                # deadlock between two concurrent syncs into one base.
                await recompute_freshness(
                    db, tenant_id=tenant_id, kb_id=kb_id, source_ids={data_source.id}
                )
        except Exception as exc:
            bookkeeping_error = f"bookkeeping failed: {exc}"
        else:
            # `recompute_freshness` writes `doc_count` with a set-based UPDATE,
            # so without this the object the caller is holding would keep
            # reporting the count from before this sync.
            await db.refresh(data_source, ["doc_count"])

    if connector is not None:
        # AFTER the cursor write-back, where §4.1 put it before it. The
        # observation can refuse a listing, and a refusal appends
        # `knowledge.reconcile_held`, which takes the tenant's audit advisory
        # lock -- so run first it made a held sync acquire that lock and THEN
        # write `knowledge_base` in the freshness recount, while every operator
        # path writes `knowledge_base` and then appends. That is the module lock
        # order of `tombstone.py` backwards, and it deadlocked. Nothing in the
        # observation reads the cursor or the counters, so moving it costs
        # nothing. It still stamps facts on rows and never deletes, and it is
        # still handed the CONNECTOR-level fatal rather than the job status: one
        # unsupported content type makes a job "partial" and says nothing at all
        # about whether the listing was complete.
        try:
            async with db.begin_nested():
                stats["reconcile"] = await observe_listing(
                    db,
                    tenant_id=tenant_id,
                    data_source=data_source,
                    kb_id=kb_id,
                    connector=connector,
                    auth=auth,
                    fatal=fatal,
                    now=now,
                )
        except Exception as exc:
            # `observe_listing` already swallows everything the CONNECTOR can do
            # to it; what is left is the database refusing the transaction, and
            # a mark that was not written costs nothing but a later sync.
            stats["reconcile"] = "error"
            bookkeeping_error = bookkeeping_error or f"reconciliation failed: {exc}"

    if bookkeeping_error is not None:
        # Recorded as an error rather than swallowed: the documents are kept, and
        # the job is the honest record that the cursor or the counters did not
        # move. The status ladder below then reads "partial" -- work landed,
        # something did not.
        errors.append(bookkeeping_error)

    if fatal is not None:
        stats["error"] = fatal
        job.status = "failed"
    elif errors and stats["ingested"] == 0:
        job.status = "failed"
    elif errors:
        job.status = "partial"
    else:
        job.status = "succeeded"
    if errors:
        stats["errors"] = errors
    job.stats = dict(stats)
    # Stamped on the SOURCE, not just the job: `connected` is transport
    # reachability and does not move on a sync failure, so without this the
    # only place a failed sync was ever visible was a job row the UI had to
    # already be polling -- gone the moment the toast that read it scrolled
    # away. "partial" counts as failed here too: something in it errored, and
    # that error is exactly what a user re-opening this source needs to see.
    if job.status == "succeeded":
        data_source.last_sync_status = "ok"
        data_source.last_sync_error = None
    else:
        data_source.last_sync_status = "failed"
        data_source.last_sync_error = fatal or "; ".join(errors) or "sync failed"
    record_ingestion_job(job.status)
    await db.flush()
    return job


def check_embedding_fits(embedding: list[float] | None, *, model: str) -> list[float] | None:
    """Refuse an embedding the column cannot hold, in words.

    A dimension mismatch otherwise surfaces as a raw driver error inside a 500 --
    `expected 1536 dimensions, not 768` -- which names neither the model that
    produced it nor the setting to change. Live, 2026-07-28, that mismatch made
    the knowledge base unusable out of the box and took a database log to
    diagnose.
    """
    if embedding is None:
        return None
    if len(embedding) != EMBED_DIM:
        raise IngestionError(
            f"the embedding model {model!r} produces {len(embedding)}-dimension "
            f"vectors, but this deployment stores {EMBED_DIM}. Point "
            "OC8_DEFAULT_EMBEDDING_MODEL at a model of the right size, or change "
            "EMBED_DIM and re-embed everything already stored."
        )
    return embedding


async def ingest_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    filename: str,
    content: str,
    content_type: str,
) -> m.IngestionJob:
    """Upload-path wrapper: build a RawDocument for the uploaded file and drive
    it through the shared ingestion tail, preserving the original job/freshness
    bookkeeping and error semantics."""
    kb = await db.get(m.KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        # A deleted base and a base that never existed answer identically, and
        # both refuse. Measured before this line: DELETE the base, then upload
        # into it -> 200, and the document was retrievable by every granted agent
        # while the operator could not list it, could not delete it and could not
        # see the base at all. The only removal path left was raw SQL against
        # `kb_chunk` -- the thing this slice exists to end.
        raise KnowledgeBaseNotFoundError("knowledge base not found")
    if (kb.index_type or "internal") != "internal":
        raise IngestionError(
            "this knowledge base is connected to an external vector index — "
            "ingest and sync are not supported; search the remote collection instead"
        )

    data_source = m.DataSource(
        tenant_id=tenant_id, connector_type="upload", name=filename, connected=True
    )
    db.add(data_source)
    await db.flush()
    job = m.IngestionJob(
        tenant_id=tenant_id, data_source_id=data_source.id, kb_id=kb_id, status="running"
    )
    db.add(job)
    await db.flush()

    try:
        extracted = extract_text(content=content, content_type=content_type)
    except IngestionError as exc:
        job.status = "failed"
        job.stats = {"error": str(exc)}
        data_source.last_sync_status = "failed"
        data_source.last_sync_error = str(exc)
        record_ingestion_job(job.status)
        await db.flush()
        raise

    if len(extracted) > MAX_DOCUMENT_LENGTH:
        error = f"document exceeds {MAX_DOCUMENT_LENGTH} characters"
        job.status = "failed"
        job.stats = {"error": error}
        data_source.last_sync_status = "failed"
        data_source.last_sync_error = error
        record_ingestion_job(job.status)
        await db.flush()
        raise IngestionError(error)

    raw = RawDocument(
        source_uri=f"upload://{data_source.id}/{filename}",
        title=filename,
        content=content,
        content_type=content_type,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        metadata={"filename": filename},
    )
    n = await ingest_raw_document(
        db, tenant_id=tenant_id, data_source=data_source, kb_id=kb_id, raw=raw
    )

    fresh: dict[str, Any] = dict(kb.freshness or {})
    fresh["docs"] = int(fresh.get("docs", 0)) + 1
    fresh["chunks"] = int(fresh.get("chunks", 0)) + n
    fresh["updated"] = datetime.now(tz=UTC).isoformat()
    kb.freshness = fresh

    data_source.doc_count = 1
    data_source.last_sync_at = datetime.now(tz=UTC).isoformat()
    data_source.last_sync_status = "ok"
    data_source.last_sync_error = None

    job.status = "succeeded"
    job.stats = {"chunks": n, "chars": len(extracted)}
    record_ingestion_job(job.status)
    await db.flush()
    return job
