"""Background ingestion worker (§11, §14.1).

Reuses the durable-run machinery wholesale: a dedicated Redis stream via the
already-parameterized RunQueue, and run_worker's already-parameterized handler
slot. On this stream the RunMessage `run_id` field carries the IngestionJob id.

Two kinds of job share it: a connector sync (`run_source_sync`) and the
indexing of one large chat file (`ingest_chat_file`, spec §5.4), told apart
by `IngestionJob.stats["kind"]`.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.files_kb import (
    CHAT_FILE_JOB,
    chat_file_uri,
    chat_local_reason,
    finish_chat_file,
    lock_chat_source,
)
from oc8.config import get_settings
from oc8.db.session import tenant_session
from oc8.knowledge.connectors.base import RawDocument
from oc8.knowledge.ingest import IngestionError, ingest_raw_document, run_source_sync
from oc8.runtime.queue import RunMessage, RunQueue
from oc8.storage import s3
from oc8.storage.attachments import full_text_key
from oc8.tenants.activity import TENANT_INACTIVE_REASON, tenant_may_run

logger = logging.getLogger(__name__)

_INGESTION_STREAM = "oc8:ingestion:stream"
_INGESTION_GROUP = "ingest"
_queue: RunQueue | None = None


def get_ingestion_queue() -> RunQueue:
    global _queue
    if _queue is None:
        _queue = RunQueue(get_settings().redis_url, key=_INGESTION_STREAM, group=_INGESTION_GROUP)
    return _queue


async def _live_base(db: AsyncSession, kb_id: uuid.UUID) -> bool:
    """Is the target base still there? A deleted one is not a place to write."""
    kb = await db.get(m.KnowledgeBase, kb_id)
    return kb is not None and kb.deleted_at is None


async def ingest_job(message: RunMessage) -> None:
    """Run one queued IngestionJob.

    Split into two sequential tenant_session transactions rather than one:
    the tenant GUC that backs RLS (`SET LOCAL app.tenant_id`) is
    transaction-local, so it cannot survive a mid-session commit -- that's
    why `run_source_sync` is flush-only. But the sync loop can run for a
    long time, and if the whole `queued -> running` flip only commits when
    the loop finishes, a reclaiming consumer (xautoclaim after the stream's
    idle window) sees the job as still `queued` under READ COMMITTED and
    `recover_ingestion` re-runs it concurrently -- duplicate KbChunk rows
    and a double cursor advance.

    So: claim first, in its own short transaction that commits before the
    long loop starts (session 1), then run the sync in a fresh transaction
    (session 2). Once session 1 commits, any reclaimer observes `running`
    and `recover_ingestion` correctly fails the job instead of re-running it.
    """
    job_id = uuid.UUID(message["run_id"])  # this stream carries the job id here
    tenant_id = uuid.UUID(message["tenant_id"])

    async with tenant_session(tenant_id) as db:
        job = await db.get(m.IngestionJob, job_id)
        chat_file = job is not None and _is_chat_file_job(job)
    if chat_file:
        await ingest_chat_file(message)
        return

    async with tenant_session(tenant_id) as db:
        job = await db.get(m.IngestionJob, job_id)
        if job is None or job.status != "queued":
            # already handled, or a redelivery of a finished job -- no-op
            return
        if not await tenant_may_run(tenant_id):
            # The organisation may not run background work: drop the job
            # rather than leave it queued for a later redelivery.
            job.status = "failed"
            job.stats = {**(job.stats or {}), "error": TENANT_INACTIVE_REASON}
            return  # tenant_session commits the failed job
        ds = await db.get(m.DataSource, job.data_source_id)
        if ds is None or ds.deleted_at is not None:
            # A job queued before the source was deleted, or a redelivered
            # stream entry, would otherwise re-ingest straight back into the
            # base whose tombstones just landed.
            job.status = "failed"
            job.stats = {**(job.stats or {}), "error": "data source no longer exists"}
            return  # tenant_session commits the failed job
        if job.kb_id is None or not await _live_base(db, job.kb_id):
            # The BASE, not only the source. `sync_source` refuses a deleted base
            # at the edge and says it is "refused here and again in
            # worker.ingest_job" -- which it was not: a job queued before the
            # base was deleted landed after it and wrote live chunks into a base
            # the operator can no longer list, delete or even see.
            job.status = "failed"
            job.stats = {**(job.stats or {}), "error": "job has no knowledge base"}
            return  # tenant_session commits the failed job
        job.status = "running"
        # tenant_session commits the claim here, before the (potentially
        # long) sync loop runs, so a stale-entry reclaim sees `running`.

    async with tenant_session(tenant_id) as db:
        job = await db.get(m.IngestionJob, job_id)
        if job is None or job.status != "running":
            # a reclaimer already failed it (or something else changed it)
            # between the two transactions -- do not run the sync twice.
            return
        ds = await db.get(m.DataSource, job.data_source_id)
        if (
            ds is None
            or ds.deleted_at is not None
            or job.kb_id is None
            or not await _live_base(db, job.kb_id)
        ):
            # Both re-checked here and not only in session 1: session 1 commits
            # before the (potentially long) sync starts, so an operator DELETE of
            # the source or of the base lands in exactly this window.
            job.status = "failed"
            job.stats = {**(job.stats or {}), "error": "data source no longer exists"}
            return  # tenant_session commits the failed job
        await run_source_sync(db, tenant_id=tenant_id, data_source=ds, kb_id=job.kb_id, job=job)


async def recover_ingestion(message: RunMessage) -> None:
    """A redelivered entry after a worker crash. queued -> run it; running ->
    mark failed (partial writes are committed and the cursor advanced, so a
    blind re-run would double-ingest -- visibly failed beats double-ingested);
    terminal/missing -> nothing to do. Mirrors executor.recover_reclaimed.

    One difference, and it is not an oversight: `recover_reclaimed` refuses to
    fail a RUNNING run whose heartbeat is fresh, because a reclaim only proves a
    consumer stopped talking to Redis. There is no such second fact here -- an
    IngestionJob has no heartbeat; its `updated_at` is when it was CLAIMED, so
    reading it would fail every honest sync that runs longer than the window,
    which is exactly the defect that made all this necessary (run 019fc303,
    2026-08-02). So this arm still decides on one fact, and the exposure is a
    ten-minute Redis partition during a live sync: the job is marked failed
    while it goes on writing. Left alone rather than papered over -- giving
    ingestion a real heartbeat is the fix, and it is a slice of its own.
    Since 2026-08-02 the worker renews its claim while `ingest_job` runs, so
    reaching this at all now takes a dead worker or that partition, where it
    used to take five minutes of ordinary work."""
    job_id = uuid.UUID(message["run_id"])
    tenant_id = uuid.UUID(message["tenant_id"])
    async with tenant_session(tenant_id) as db:
        job = await db.get(m.IngestionJob, job_id)
        if job is None or job.status in ("succeeded", "failed", "partial"):
            return
        if job.status == "running":
            job.status = "failed"
            job.stats = {**(job.stats or {}), "error": "worker died mid-ingestion"}
            if _is_chat_file_job(job):
                # Its file is handed over inline from now on.
                await finish_chat_file(
                    db, attachment_id=uuid.UUID(job.stats["attachment_id"]), status="failed"
                )
            logger.warning("ingestion job %s marked failed: lease lost", job_id)
            return  # tenant_session commits
    # queued: run it in a fresh session, like recover_reclaimed defers to execute_run
    await ingest_job(message)


def _is_chat_file_job(job: m.IngestionJob) -> bool:
    return (job.stats or {}).get("kind") == CHAT_FILE_JOB


class _ChatFileGone(Exception):
    """The chat's base or source, or the file itself, went away mid-ingest."""


async def _fail_chat_file(
    tenant_id: uuid.UUID, job_id: uuid.UUID, attachment_id: uuid.UUID, error: str
) -> None:
    async with tenant_session(tenant_id) as db:
        job = await db.get(m.IngestionJob, job_id)
        if job is None or job.status not in ("queued", "running"):
            return
        job.status = "failed"
        job.stats = {**(job.stats or {}), "error": error}
        await finish_chat_file(db, attachment_id=attachment_id, status="failed")


async def ingest_chat_file(message: RunMessage) -> None:
    """Index one large chat file into its chat's own knowledge base.

    Claimed in a short transaction of its own first, for the reason
    `ingest_job` gives. Then the file's full text (stored beside it at upload)
    is chunked and embedded under `chat_file_max_chars`, with a vector for
    every chunk or not at all: a model that is down, or an embedding policy
    that keeps the text off every model there is (a local-only base, a
    restricted label), fails the job and the file is handed to the model
    inline and truncated, as before. When the chat's agent runs on a local
    model, only a local embedding model may embed the file
    (`chat.files_kb.chat_local_reason`): a cloud one is refused and the file
    goes inline.

    Before its chunks commit, the chat's source row is locked and re-checked
    (`chat.files_kb.lock_chat_source`, the mutex with `drop_chat_kb`): the
    chat was deleted meanwhile -> the whole transaction rolls back, the job
    fails, and nothing is left under the dropped base. The lock is taken
    only then, not before the embedding, so deleting a chat never waits for
    minutes of embedding calls.
    """
    job_id = uuid.UUID(message["run_id"])
    tenant_id = uuid.UUID(message["tenant_id"])

    async with tenant_session(tenant_id) as db:
        job = await db.get(m.IngestionJob, job_id)
        if job is None or job.status != "queued":
            return
        attachment_id = uuid.UUID(job.stats["attachment_id"])
        error: str | None = None
        attachment = await db.get(m.FileAttachment, attachment_id)
        ds = await db.get(m.DataSource, job.data_source_id)
        if not await tenant_may_run(tenant_id):
            error = TENANT_INACTIVE_REASON
        elif ds is None or ds.deleted_at is not None:
            error = "data source no longer exists"
        elif job.kb_id is None or not await _live_base(db, job.kb_id):
            error = "job has no knowledge base"
        elif attachment is None:
            error = "the file no longer exists"
        if error is not None:
            job.status = "failed"
            job.stats = {**(job.stats or {}), "error": error}
            await finish_chat_file(db, attachment_id=attachment_id, status="failed")
            return  # tenant_session commits the failed job
        assert attachment is not None and job.kb_id is not None
        kb_id, source_id = job.kb_id, job.data_source_id
        base = await db.get(m.KnowledgeBase, kb_id)
        chat = (
            await db.get(m.ChatSession, base.chat_session_id)
            if base is not None and base.chat_session_id is not None
            else None
        )
        # The chat's agent on a local model -> no cloud embedding model sees
        # its files; with no local one configured the file goes inline.
        local_reason = await chat_local_reason(db, session=chat)
        text_key, filename = full_text_key(attachment), attachment.filename
        job.status = "running"

    try:
        content = (await s3.get_object(text_key)).decode("utf-8")
        async with tenant_session(tenant_id) as db:
            job = await db.get(m.IngestionJob, job_id)
            if job is None or job.status != "running":
                return  # a reclaimer failed it meanwhile
            ds = await db.get(m.DataSource, source_id)
            if ds is None:
                raise _ChatFileGone("data source no longer exists")
            chunks = await ingest_raw_document(
                db,
                tenant_id=tenant_id,
                data_source=ds,
                kb_id=kb_id,
                raw=RawDocument(
                    source_uri=chat_file_uri(attachment_id),
                    title=filename,
                    content=content,
                    content_type="text/plain",
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    metadata={"attachment_id": str(attachment_id), "filename": filename},
                ),
                max_chars=get_settings().chat_file_max_chars,
                require_embeddings=True,
                require_local=local_reason,
            )
            if await lock_chat_source(db, source_id=source_id) is None:
                raise _ChatFileGone("the chat was deleted while its file was indexed")
            kb = (
                await db.execute(
                    select(m.KnowledgeBase)
                    .where(m.KnowledgeBase.id == kb_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if kb is None or kb.deleted_at is not None:
                raise _ChatFileGone("the chat was deleted while its file was indexed")
            if not await finish_chat_file(db, attachment_id=attachment_id, status="indexed"):
                raise _ChatFileGone("the file no longer exists")
            job.status = "succeeded"
            job.stats = {**(job.stats or {}), "chunks": chunks, "chars": len(content)}
    except (IngestionError, _ChatFileGone, s3.ObjectNotFound) as exc:
        logger.info("chat file %s not indexed: %s", attachment_id, exc)
        await _fail_chat_file(tenant_id, job_id, attachment_id, str(exc) or type(exc).__name__)
    except Exception:
        logger.exception("chat file %s: indexing failed", attachment_id)
        await _fail_chat_file(tenant_id, job_id, attachment_id, "indexing failed")
