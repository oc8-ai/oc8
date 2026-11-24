"""A chat's own knowledge base: where its large files are made searchable
(spec §5.4).

One hidden base per chat session (`KnowledgeBase.kind == KB_KIND_CHAT`,
`chat_session_id` set), created lazily the first time the chat gets a file
too large to hand to the model inline, and fed by one `DataSource` of its own
(`CHAT_UPLOAD_CONNECTOR`), named in the base's `source_ids`.

Hidden means: never in a knowledge-base list, picker, grant, context search or
an agent's normal retrieval -- each of those filters on `KB_KIND_SHARED`, and
`retrieve_kb_context` searches a chat base only when its caller names it in
`only_kb_ids`. Whoever may read the chat may search its files; no
`knowledge:*` right is involved, so nothing here checks one.

A chat file whose extracted text is above `INLINE_ATTACHMENT_CHARS` is queued
at upload (`enqueue_chat_file_ingest`) as an `IngestionJob` of kind
`CHAT_FILE_JOB` on the ingestion stream, and ingested by the worker
(`knowledge.worker.ingest_chat_file`); the turn gets a note instead of the
whole text (`attachment_turn_text`). Whenever indexing fails the file is
handed to the model inline and truncated, as before, with a visible note.

**Locks.** A chat's `chat_upload` source row is the mutex between its ingest
and its removal: `drop_chat_kb` locks it before it erases anything, and the
ingest locks it after flushing its chunks and before committing them, then
gives up if the source is gone. Either the drop sees the committed chunks and
erases them, or the ingest sees the deleted source and rolls its chunks back;
no chunk commits unerased under a dropped base. That puts data_source FIRST
for a chat's own source, ahead of `knowledge.tombstone`'s kb_chunk ->
data_source -> knowledge_base. It cannot invert against that order: the only
writers of a chat source's chunks are this ingest (whose chunks are new rows
no one else can hold) and `drop_chat_kb` -- the knowledge API, the sweep and
reconciliation never reach a chat's base or its source. A file row
(`file_attachment`) comes after the knowledge rows and before the tenant's
audit lock (step 4 of `chat/tree.py`'s order).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import Principal
from oc8.chat.tree import path_to, visible_path
from oc8.config import get_settings
from oc8.knowledge.embedding_policy import LOCAL_AGENT_REASON, agent_runs_locally
from oc8.knowledge.ingest import MAX_DOCUMENT_LENGTH
from oc8.knowledge.tombstone import (
    OPERATOR_DELETE,
    tombstone_base,
    tombstone_document,
    tombstone_source,
)
from oc8.models.knowledge import CHAT_UPLOAD_CONNECTOR, KB_KIND_CHAT
from oc8.storage import s3
from oc8.storage.attachments import INDEXED_STATUSES, INLINE_ATTACHMENT_CHARS, full_text_key

logger = logging.getLogger(__name__)

#: `IngestionJob.stats["kind"]` of a job that indexes one chat file.
CHAT_FILE_JOB = "chat_file"
#: How much of an indexed file the turn's note shows.
PREVIEW_CHARS = 2_000
TRUNCATION_NOTE = "\n(Note: this file was cut at 200,000 characters.)"


async def _live_chat_kb(db: AsyncSession, session: m.ChatSession) -> m.KnowledgeBase | None:
    return (
        await db.execute(
            select(m.KnowledgeBase).where(
                m.KnowledgeBase.tenant_id == session.tenant_id,
                m.KnowledgeBase.kind == KB_KIND_CHAT,
                m.KnowledgeBase.chat_session_id == session.id,
                m.KnowledgeBase.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def chat_kb_source(db: AsyncSession, *, kb: m.KnowledgeBase) -> m.DataSource | None:
    """The one source that feeds a chat's base, while it is live."""
    for source_id in kb.source_ids or []:
        source = await db.get(m.DataSource, uuid.UUID(str(source_id)))
        if (
            source is not None
            and source.tenant_id == kb.tenant_id
            and source.connector_type == CHAT_UPLOAD_CONNECTOR
            and source.deleted_at is None
        ):
            return source
    return None


async def chat_kb_for(
    db: AsyncSession, *, session: m.ChatSession, create: bool
) -> m.KnowledgeBase | None:
    """The chat's live base, or None. With `create`, one is made when there is
    none: the deployment's default embedding model, `internal`, and a source of
    its own. Not `local_only` even for an agent on a local model: that would
    also keep the files' text from every run on a cloud model. Where the text
    may be embedded is decided per ingest instead (`chat_local_reason`), and
    a search's question by `retrieve_kb_context`'s own agent rule.

    Two uploads racing to create it both succeed: the loser's insert hits
    `uq_knowledge_base_chat_session` inside a savepoint, which is rolled back
    (its source with it), and it returns the winner's base. Flush-only; the
    caller owns the commit."""
    kb = await _live_chat_kb(db, session)
    if kb is not None or not create:
        return kb
    try:
        async with db.begin_nested():
            source = m.DataSource(
                tenant_id=session.tenant_id,
                connector_type=CHAT_UPLOAD_CONNECTOR,
                name="Chat files",
                config={"chat_session_id": str(session.id)},
                classification="internal",
                connected=True,
            )
            db.add(source)
            await db.flush()
            kb = m.KnowledgeBase(
                tenant_id=session.tenant_id,
                name="Chat files",
                description="",
                embedding_model=get_settings().default_embedding_model,
                classification="internal",
                status="current",
                source_ids=[str(source.id)],
                kind=KB_KIND_CHAT,
                chat_session_id=session.id,
            )
            db.add(kb)
            await db.flush()
    except IntegrityError as exc:
        # Only the race this is for: another upload created the base first.
        # Anything else -- or that race with no base to show for it -- is a
        # real error.
        if "uq_knowledge_base_chat_session" not in str(exc.orig):
            raise
        winner = await _live_chat_kb(db, session)
        if winner is None:
            raise
        return winner
    return kb


#: Why a chat whose agent cannot be found keeps its files on a local model.
UNKNOWN_AGENT_REASON = "the chat's agent is unknown, so its files stay on a local model"


async def chat_local_reason(db: AsyncSession, *, session: m.ChatSession | None) -> str | None:
    """Why this chat's files may only be embedded by a local model, or None.

    The embedding policy agents already follow (`knowledge.embedding_policy`):
    an agent on a local model may be handed restricted material that chat keeps
    off cloud models, so what it reads -- here, the files of its chat -- is
    embedded locally or not at all (`LOCAL_AGENT_REASON`). Read from the
    agent's live row each time: the base is created at the first upload, the
    file is ingested later, and the agent may have moved to a local model in
    between. Fails closed: no session or no agent -> local only."""
    agent = await db.get(m.Agent, session.agent_id) if session is not None else None
    if agent is None:
        return UNKNOWN_AGENT_REASON
    return LOCAL_AGENT_REASON if await agent_runs_locally(db, agent, None) else None


async def lock_chat_source(db: AsyncSession, *, source_id: uuid.UUID) -> m.DataSource | None:
    """Lock a chat's `chat_upload` source row (the mutex between its ingest
    and its removal -- see the module docstring) and return it while it is
    live; None once it was deleted. Re-read, never the session's cached copy."""
    source = (
        await db.execute(
            select(m.DataSource)
            .where(m.DataSource.id == source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source is None or source.deleted_at is not None:
        return None
    return source


async def drop_chat_kb(db: AsyncSession, *, session: m.ChatSession) -> None:
    """Soft-delete the chat's base and erase its files from it, through the
    same paths an operator's DELETE takes (`knowledge.tombstone`): the content
    is destroyed now, each row stays as its own tombstone, and the removal is
    in the audit chain.

    The source row is locked before anything is erased: it is the mutex with
    a background ingest of this chat's files (module docstring), so an ingest
    that already holds it commits first and its chunks are erased here, and
    one that comes later finds the source deleted and rolls back. Then the
    source, then the base: `tombstone_source` takes kb_chunk, data_source
    (already held) and knowledge_base (its recount); `tombstone_base` then
    finds nothing left to erase and retires the base. Done the other way
    round, a chat whose base holds no chunk yet would write knowledge_base
    before data_source. The audit lock comes at commit, after all of these
    rows (chat/tree.py, step 4). Flush-only."""
    kb = await _live_chat_kb(db, session)
    if kb is None:
        return
    now = dt.datetime.now(tz=dt.UTC)
    source = await chat_kb_source(db, kb=kb)
    if source is not None:
        source = await lock_chat_source(db, source_id=source.id)
    if source is not None:
        await tombstone_source(db, tenant_id=session.tenant_id, data_source=source, now=now)
    await tombstone_base(db, tenant_id=session.tenant_id, kb=kb, now=now)


def attachment_turn_text(att: m.FileAttachment) -> str:
    """What a turn says about one of its (non-image) files.

    Small, never indexed, or indexing failed: the file's text inline, as
    always, with a visible note when it was cut at `MAX_DOCUMENT_LENGTH` (a
    file of exactly that length gets the note too; the row does not say
    whether anything was cut). Queued or indexed: name, size, the first
    `PREVIEW_CHARS` characters and how to reach the rest -- the turn never
    waits for indexing; the tools say "still indexing" until it is done."""
    label = f"\n\n[Attached file: {att.filename}]\n"
    content = att.extracted_text
    if not content:
        return label + "(could not read this file's content)"
    if len(content) <= INLINE_ATTACHMENT_CHARS or att.index_status not in INDEXED_STATUSES:
        cut = TRUNCATION_NOTE if len(content) >= MAX_DOCUMENT_LENGTH else ""
        return label + content + cut
    return (
        f"{label}(A large file, {att.size_bytes:,} bytes. Its first {PREVIEW_CHARS:,} "
        f"characters:)\n{content[:PREVIEW_CHARS]}\n[...]\n"
        "The full file is searchable with search_chat_files and readable page by page "
        f"with read_chat_file (file id {att.id})."
    )


async def chat_has_files(db: AsyncSession, context: Mapping[str, Any] | None) -> bool:
    """Whether a run's chat (`context["chat_session_id"]`) has a live base of
    its own -- what makes `search_chat_files` and `read_chat_file` worth
    offering. Who may use them is decided when they are called."""
    raw = (context or {}).get("chat_session_id")
    try:
        session_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return False
    session = await db.get(m.ChatSession, session_id)
    return session is not None and await _live_chat_kb(db, session) is not None


async def chat_files_on_path(
    db: AsyncSession, *, session: m.ChatSession, anchor_id: uuid.UUID | None
) -> list[m.FileAttachment]:
    """The (non-image) files of the messages from the chat's root to
    `anchor_id`, the turn a run answers (`chat.tree.path_to`); the person's
    visible path when there is no anchor. A file on another branch, one not
    sent yet, or one of another chat is not among them."""
    path = (
        await path_to(db, session=session, message_id=anchor_id)
        if anchor_id is not None
        else await visible_path(db, session=session)
    )
    message_ids = [msg.id for msg in path]
    if not message_ids:
        return []
    return list(
        (
            await db.execute(
                select(m.FileAttachment)
                .where(
                    m.FileAttachment.tenant_id == session.tenant_id,
                    m.FileAttachment.owner_type == "chat_message",
                    m.FileAttachment.owner_id.in_(message_ids),
                    m.FileAttachment.is_image.is_(False),
                )
                .order_by(m.FileAttachment.id)
            )
        ).scalars()
    )


def chat_file_root(attachment: m.FileAttachment) -> uuid.UUID:
    """The file whose index a chat file's chunks belong to: the original for an
    edited turn's copy (`indexed_from_id`), else the file itself."""
    return attachment.indexed_from_id or attachment.id


async def load_chat_file_text(attachment: m.FileAttachment) -> str | None:
    """A chat file's whole extracted text: for a large file the full text
    stored beside it (not the capped `extracted_text`), else `extracted_text`.
    None when there is none to read."""
    if attachment.index_status not in INDEXED_STATUSES:
        return attachment.extracted_text
    try:
        raw = await s3.get_object(full_text_key(attachment))
    except s3.ObjectNotFound:
        return None
    return raw.decode("utf-8")


async def finish_chat_file(db: AsyncSession, *, attachment_id: uuid.UUID, status: str) -> bool:
    """Set the indexing result on a chat file and on every copy sharing its
    index (an edited turn's copy, `storage.attachments.copy_attachment`).

    The original's row is locked first -- the same lock a copy takes to read
    its status -- and the copies are then updated in a statement of their
    own, which sees every copy committed while this waited for the lock.
    Returns whether the original still exists. Flush-only."""
    root = (
        await db.execute(
            select(m.FileAttachment.id)
            .where(m.FileAttachment.id == attachment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    await db.execute(
        update(m.FileAttachment)
        .where(
            or_(
                m.FileAttachment.id == attachment_id,
                m.FileAttachment.indexed_from_id == attachment_id,
            ),
            m.FileAttachment.index_status == "queued",
        )
        .values(index_status=status)
        .execution_options(synchronize_session=False)
    )
    return root is not None


async def enqueue_chat_file_ingest(
    db: AsyncSession, *, attachment: m.FileAttachment, session: m.ChatSession
) -> None:
    """Queue the background ingest of a large chat file into the chat's own
    base (created here if it has none). Does nothing for a file that is not
    waiting for it (`index_status != 'queued'`: small, unreadable, an image,
    or above `chat_file_max_chars`).

    COMMITS: the job row must be durable before its stream entry exists, or
    the worker can read the entry first and find no job (the same rule as
    `api/v1/knowledge.py`'s `sync_source`). The tenant binding is restored
    after each commit, so the caller can go on using `db`. When the queue
    cannot take the entry, the file is marked failed and is handed to the
    model inline instead -- an upload never fails for want of indexing."""
    if attachment.index_status != "queued":
        return
    from oc8.knowledge.worker import get_ingestion_queue  # the worker imports this module

    tenant_id = session.tenant_id
    kb = await chat_kb_for(db, session=session, create=True)
    source = await chat_kb_source(db, kb=kb) if kb is not None else None
    if kb is None or source is None:
        attachment.index_status = "failed"
        await db.flush()
        return
    job = m.IngestionJob(
        tenant_id=tenant_id,
        data_source_id=source.id,
        kb_id=kb.id,
        status="queued",
        stats={"kind": CHAT_FILE_JOB, "attachment_id": str(attachment.id)},
    )
    db.add(job)
    await db.flush()
    await _commit(db, tenant_id)
    try:
        await get_ingestion_queue().enqueue(run_id=job.id, tenant_id=tenant_id)
    except Exception:
        logger.warning(
            "could not queue chat file %s for indexing; it is handed over inline",
            attachment.id,
            exc_info=True,
        )
        job.status = "failed"
        job.stats = {**job.stats, "error": "could not be queued"}
        await finish_chat_file(db, attachment_id=attachment.id, status="failed")
        await db.refresh(attachment, ["index_status"])
        await _commit(db, tenant_id)


async def _commit(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    await db.commit()
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )


def chat_file_uri(attachment_id: uuid.UUID) -> str:
    """The `source_uri` a chat file's chunks carry: the id of the file whose
    index they are (the original, never a copy)."""
    return f"chat-file://{attachment_id}"


async def _chat_of(db: AsyncSession, attachment: m.FileAttachment) -> m.ChatSession | None:
    """The chat a chat file belongs to: `owner_id` is the session until the
    turn is sent, the message after (see `api/v1/files.py`)."""
    session = await db.get(m.ChatSession, attachment.owner_id)
    if session is not None:
        return session
    message = await db.get(m.ChatMessage, attachment.owner_id)
    return await db.get(m.ChatSession, message.session_id) if message is not None else None


async def forget_chat_file(
    db: AsyncSession, *, attachment: m.FileAttachment, principal: Principal | None
) -> None:
    """Delete a chat file's row and take its text out of the chat's base: a
    file someone deleted -- removed before sending, or deleted after -- must
    not stay searchable.

    Locks in the order of everything else that writes a chat's base (module
    docstring): its source first; then the original file's row, which is
    where the ingest (`finish_chat_file`) and an edit's copy take it, so the
    count of files sharing the index cannot miss a copy; then the file's
    chunks (`tombstone_document`, reduced now, as an operator delete is),
    then the file row, then its job. kb_chunk after file_attachment cannot
    invert against anything: the only other writers of a chat's chunks hold
    its source first.
    The chunks stay while another row still shares the index -- the original
    or an edited turn's copy (`indexed_from_id`) -- and go with the last of
    them. An ingest still queued or running for the file is failed: the worker
    finds the file gone and rolls back (`knowledge.worker.ingest_chat_file`).
    Flush-only; the caller deletes the blobs and commits."""
    root_id = attachment.indexed_from_id or attachment.id
    shared = attachment.index_status in INDEXED_STATUSES
    if shared:
        chat = await _chat_of(db, attachment)
        kb = await chat_kb_for(db, session=chat, create=False) if chat is not None else None
        source = await chat_kb_source(db, kb=kb) if kb is not None else None
        source = await lock_chat_source(db, source_id=source.id) if source is not None else None
        # The original's row next, the lock `copy_attachment` reads its status
        # under: a copy made meanwhile is either counted below or finds the
        # original gone and falls back inline.
        await db.execute(
            select(m.FileAttachment.id).where(m.FileAttachment.id == root_id).with_for_update()
        )
        others = (
            await db.execute(
                select(func.count())
                .select_from(m.FileAttachment)
                .where(
                    or_(
                        m.FileAttachment.id == root_id,
                        m.FileAttachment.indexed_from_id == root_id,
                    ),
                    m.FileAttachment.id != attachment.id,
                )
            )
        ).scalar_one()
        shared = others > 0
        if not shared and kb is not None and source is not None:
            await tombstone_document(
                db,
                tenant_id=attachment.tenant_id,
                kb_id=kb.id,
                data_source_id=source.id,
                source_uri=chat_file_uri(root_id),
                reason=OPERATOR_DELETE,
                reduce_now=True,
                now=dt.datetime.now(tz=dt.UTC),
                principal=principal,
            )
    await db.delete(attachment)
    await db.flush()
    if attachment.index_status in INDEXED_STATUSES and not shared:
        jobs = (
            await db.execute(
                select(m.IngestionJob).where(
                    m.IngestionJob.tenant_id == attachment.tenant_id,
                    m.IngestionJob.stats["attachment_id"].astext == str(root_id),
                    m.IngestionJob.status.in_(("queued", "running")),
                )
            )
        ).scalars()
        for job in jobs:
            job.status = "failed"
            job.stats = {**(job.stats or {}), "error": "the file was deleted"}
        await db.flush()
