"""Large chat files are indexed in the background instead of truncated (spec
§5.4): a file whose extracted text is above the inline threshold is queued at
upload, the worker ingests it into the chat's own knowledge base, and the turn
gets a short note instead of the whole text. Whenever indexing cannot happen --
the embedding model is down, the policy keeps the text off it, the file is
above the chat-file ceiling -- the turn falls back to the inline, truncated
text with a visible note."""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.chat.files_kb import (
    attachment_turn_text,
    chat_kb_for,
    chat_kb_source,
    drop_chat_kb,
    load_chat_file_text,
    lock_chat_source,
)
from oc8.config import get_settings
from oc8.knowledge.connectors.base import RawDocument
from oc8.knowledge.embedding_policy import LOCAL_AGENT_REASON
from oc8.knowledge.ingest import MAX_DOCUMENT_LENGTH, ingest_raw_document
from oc8.knowledge.worker import ingest_job, recover_ingestion
from oc8.modelrouter import EmbeddingUnavailable
from oc8.models.knowledge import EMBED_DIM
from oc8.runtime.queue import RunMessage
from oc8.storage.attachments import INLINE_ATTACHMENT_CHARS, copy_attachment
from tests.chat.test_edit_and_switch import _client, _new_chat, _post, _seed_agent, _task, _token
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    """A local model that embeds anything; refuses only when told to be down."""

    down = False

    async def embed(self, text: str, model: str | None = None, **kw: object) -> list[float]:
        if self.down:
            raise EmbeddingUnavailable("embedding backend unreachable")
        seed = sum(ord(c) for c in text[:50]) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _CloudOnlyRouter(_FakeEmbedRouter):
    """Only cloud embedding models: anything that must stay local is refused,
    as the real router refuses it."""

    async def embed(self, text: str, model: str | None = None, **kw: object) -> list[float]:
        if kw.get("require_local"):
            raise EmbeddingUnavailable(f"content must stay on a local model: {kw['require_local']}")
        return await super().embed(text, model)


class _RecordingQueue:
    """The ingestion stream, as far as these tests drive it: each test runs the
    worker itself, so nothing is left on the shared stream for another test's
    worker to pick up."""

    def __init__(self) -> None:
        self.job_ids: list[uuid.UUID] = []

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.job_ids.append(run_id)


@pytest.fixture(autouse=True)
def queue(monkeypatch: pytest.MonkeyPatch) -> _RecordingQueue:
    recorder = _RecordingQueue()
    monkeypatch.setattr("oc8.knowledge.worker.get_ingestion_queue", lambda: recorder)
    return recorder


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> _FakeEmbedRouter:
    fake = _FakeEmbedRouter()
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: fake)
    return fake


def _text(n: int) -> str:
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet "
    return (words * (n // len(words) + 1))[:n]


async def _upload(c: Any, tenant: uuid.UUID, sid: str, name: str, body: str) -> str:
    r = await c.post(
        f"/api/v1/chat/sessions/{sid}/attachments",
        files={"file": (name, io.BytesIO(body.encode()), "text/plain")},
        headers=_token(tenant),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _attachment(app_session: AppSessionFactory, tenant: uuid.UUID, aid: str) -> Any:
    async with app_session(tenant) as db:
        row = await db.get(m.FileAttachment, uuid.UUID(aid))
        assert row is not None
        return row


async def _job_for(app_session: AppSessionFactory, tenant: uuid.UUID, aid: str) -> Any:
    async with app_session(tenant) as db:
        return (
            await db.execute(
                select(m.IngestionJob).where(m.IngestionJob.stats["attachment_id"].astext == aid)
            )
        ).scalar_one_or_none()


def _message(job: m.IngestionJob) -> RunMessage:
    return RunMessage(
        run_id=str(job.id), tenant_id=str(job.tenant_id), entry_id="0-0", redelivered=False
    )


async def _live_chunks(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.KbChunk]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.KbChunk).where(
                        m.KbChunk.tenant_id == tenant, m.KbChunk.reduced_at.is_(None)
                    )
                )
            ).scalars()
        )


async def test_small_file_stays_inline(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    router: _FakeEmbedRouter,
    queue: _RecordingQueue,
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "notes.txt", "the quarterly numbers")
        sent = await _post(c, tenant, sid, "see attached", attachmentIds=[aid])
    row = await _attachment(app_session, tenant, aid)
    assert row.index_status == "inline"
    assert await _job_for(app_session, tenant, aid) is None
    assert queue.job_ids == []
    task = await _task(app_session, tenant, sent["runId"])
    assert "[Attached file: notes.txt]\nthe quarterly numbers" in task


async def test_large_file_is_queued_at_upload_and_the_turn_gets_a_note(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    router: _FakeEmbedRouter,
    queue: _RecordingQueue,
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    body = "HEADLINE " + _text(INLINE_ATTACHMENT_CHARS + 10_000) + " TAIL-MARKER"
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", body)
        sent = await _post(c, tenant, sid, "summarise this", attachmentIds=[aid])
    row = await _attachment(app_session, tenant, aid)
    assert row.index_status == "queued"
    job = await _job_for(app_session, tenant, aid)
    assert job is not None and job.status == "queued"
    assert queue.job_ids == [job.id], "queued once, at upload"
    assert await load_chat_file_text(row) == body, "the full text sits beside the file"

    task = await _task(app_session, tenant, sent["runId"])
    assert "HEADLINE" in task and "TAIL-MARKER" not in task
    assert "report.txt" in task
    assert f"read_chat_file (file id {aid})" in task
    assert "search_chat_files" in task


async def test_job_indexes_the_file_into_the_chats_knowledge_base(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _text(INLINE_ATTACHMENT_CHARS + 5_000))
    job = await _job_for(app_session, tenant, aid)
    await ingest_job(_message(job))

    job = await _job_for(app_session, tenant, aid)
    assert job.status == "succeeded", job.stats
    row = await _attachment(app_session, tenant, aid)
    assert row.index_status == "indexed"
    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, uuid.UUID(sid))
        kb = await chat_kb_for(db, session=session, create=False)
        assert kb is not None and job.kb_id == kb.id
        source = await chat_kb_source(db, kb=kb)
        assert source is not None and job.data_source_id == source.id
        chunks = list(
            (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalars()
        )
    assert len(chunks) > 1
    assert all(ch.embedding is not None and ch.data_source_id == source.id for ch in chunks)
    assert {ch.chunk_metadata["attachment_id"] for ch in chunks} == {aid}
    # A redelivery of the finished job changes nothing.
    await ingest_job(_message(job))
    assert len(await _live_chunks(app_session, tenant)) == len(chunks)


async def test_ingest_failure_falls_back_inline(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    body = _text(MAX_DOCUMENT_LENGTH + 30_000)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "big.txt", body)
        router.down = True
        await ingest_job(_message(await _job_for(app_session, tenant, aid)))
        sent = await _post(c, tenant, sid, "what is in it?", attachmentIds=[aid])

    job = await _job_for(app_session, tenant, aid)
    assert job.status == "failed" and "embedding" in job.stats["error"]
    row = await _attachment(app_session, tenant, aid)
    assert row.index_status == "failed"
    assert await _live_chunks(app_session, tenant) == [], "nothing half-indexed is left"
    task = await _task(app_session, tenant, sent["runId"])
    assert body[:MAX_DOCUMENT_LENGTH] in task
    assert "(Note: this file was cut at 200,000 characters.)" in task
    assert "read_chat_file" not in task


async def test_embedding_policy_refusal_falls_back_inline(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _CloudOnlyRouter())
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "secret.txt", _text(INLINE_ATTACHMENT_CHARS + 1))
    job = await _job_for(app_session, tenant, aid)
    async with app_session(tenant) as db:
        kb = await db.get(m.KnowledgeBase, job.kb_id)
        kb.local_only = True
    await ingest_job(_message(job))
    row = await _attachment(app_session, tenant, aid)
    assert row.index_status == "failed"
    assert "local" in (await _job_for(app_session, tenant, aid)).stats["error"]
    assert attachment_turn_text(row).startswith("\n\n[Attached file: secret.txt]\n")
    assert "read_chat_file" not in attachment_turn_text(row)


async def test_file_above_the_chat_file_ceiling_falls_back_inline(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    router: _FakeEmbedRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "chat_file_max_chars", INLINE_ATTACHMENT_CHARS + 1_000)
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    body = _text(INLINE_ATTACHMENT_CHARS + 2_000)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "huge.txt", body)
    row = await _attachment(app_session, tenant, aid)
    assert row.index_status == "failed"
    assert await _job_for(app_session, tenant, aid) is None
    text = attachment_turn_text(row)
    assert body in text and "(Note:" not in text, "inline, and not cut below 200,000"


def _row(text: str | None, status: str | None, **kw: Any) -> m.FileAttachment:
    return m.FileAttachment(
        id=uuid.uuid4(),
        filename="f.txt",
        size_bytes=123_456,
        extracted_text=text,
        index_status=status,
        is_image=False,
        **kw,
    )


async def test_attachment_turn_text_shapes() -> None:
    assert attachment_turn_text(_row("short", "inline")) == "\n\n[Attached file: f.txt]\nshort"
    assert attachment_turn_text(_row(None, None)) == (
        "\n\n[Attached file: f.txt]\n(could not read this file's content)"
    )
    long_text = "x" * (INLINE_ATTACHMENT_CHARS + 1)
    # Not indexed (an older row, an instruction file): inline, as today.
    assert attachment_turn_text(_row(long_text, None)).endswith(long_text)
    cut = "y" * MAX_DOCUMENT_LENGTH
    assert attachment_turn_text(_row(cut, "failed")).endswith(
        cut + "\n(Note: this file was cut at 200,000 characters.)"
    )
    for status in ("queued", "indexed"):
        row = _row(long_text, status)
        note = attachment_turn_text(row)
        assert len(note) < 3_000
        assert "x" * 2_000 in note and "x" * 2_001 not in note
        assert "f.txt" in note and "123,456 bytes" in note
        assert note.endswith(
            "The full file is searchable with search_chat_files and readable page by page "
            f"with read_chat_file (file id {row.id})."
        )


async def test_drop_while_an_ingest_is_mid_flight_leaves_no_unreduced_chunks(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    router: _FakeEmbedRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat is deleted while the worker is embedding: the chunks it has
    flushed must never commit under the deleted base."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _text(INLINE_ATTACHMENT_CHARS + 5_000))
    job = await _job_for(app_session, tenant, aid)
    real = ingest_raw_document

    async def ingest_then_drop(*args: Any, **kwargs: Any) -> int:
        n = await real(*args, **kwargs)
        async with app_session(tenant) as other:
            session = await other.get(m.ChatSession, uuid.UUID(sid))
            await drop_chat_kb(other, session=session)
        return n

    monkeypatch.setattr("oc8.knowledge.worker.ingest_raw_document", ingest_then_drop)
    await ingest_job(_message(job))

    assert await _live_chunks(app_session, tenant) == []
    job = await _job_for(app_session, tenant, aid)
    assert job.status == "failed"
    assert (await _attachment(app_session, tenant, aid)).index_status == "failed"


async def test_drop_waits_for_an_ingest_holding_the_source_and_then_erases_it(
    app_session: AppSessionFactory, router: _FakeEmbedRouter
) -> None:
    """The other order: the ingest holds the source row when the drop starts,
    so the drop waits, and its erasure then sees the committed chunks."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member = m.OrgMember(tenant_id=tenant, subject="op", subject_uuid=uuid.uuid4())
        db.add(member)
        await db.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=member.id)
        db.add(session)
        await db.flush()
        kb = await chat_kb_for(db, session=session, create=True)
        source_id, kb_id, session_id = uuid.UUID(kb.source_ids[0]), kb.id, session.id

    async with app_session(tenant) as ingest:
        source = await lock_chat_source(ingest, source_id=source_id)
        assert source is not None
        await ingest_raw_document(
            ingest,
            tenant_id=tenant,
            data_source=source,
            kb_id=kb_id,
            raw=RawDocument(
                source_uri="chat-file://x",
                title="x",
                content=_text(5_000),
                content_type="text/plain",
                content_hash="h",
            ),
        )

        async def drop() -> None:
            async with app_session(tenant) as other:
                chat = await other.get(m.ChatSession, session_id)
                await drop_chat_kb(other, session=chat)

        dropping = asyncio.create_task(drop())
        await asyncio.sleep(0.5)
        assert not dropping.done(), "the drop waits for the source row"
    await dropping
    assert await _live_chunks(app_session, tenant) == []


async def test_recovered_running_chat_job_marks_the_file_failed(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
    job = await _job_for(app_session, tenant, aid)
    async with app_session(tenant) as db:
        (await db.get(m.IngestionJob, job.id)).status = "running"
    await recover_ingestion({**_message(job), "redelivered": True})
    assert (await _job_for(app_session, tenant, aid)).status == "failed"
    assert (await _attachment(app_session, tenant, aid)).index_status == "failed"


async def test_edit_copies_follow_the_original_index(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    """A copy made while the original is still queued becomes indexed with it;
    a copy of an indexed file is indexed at once. Neither is ingested again."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    body = _text(INLINE_ATTACHMENT_CHARS + 5_000)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", body)
    job = await _job_for(app_session, tenant, aid)
    async with app_session(tenant) as db:
        original = await db.get(m.FileAttachment, uuid.UUID(aid))
        queued_copy = await copy_attachment(db, original, owner_id=uuid.uuid4())
        assert queued_copy.index_status == "queued"
        assert queued_copy.indexed_from_id == original.id
        queued_copy_id = queued_copy.id
    await ingest_job(_message(job))
    async with app_session(tenant) as db:
        copied = await db.get(m.FileAttachment, queued_copy_id)
        assert copied.index_status == "indexed"
        assert await load_chat_file_text(copied) == body
        late_copy = await copy_attachment(db, copied, owner_id=uuid.uuid4())
        assert late_copy.index_status == "indexed"
        assert late_copy.indexed_from_id == original.id
    async with app_session(tenant) as db:
        jobs = (await db.execute(select(func.count()).select_from(m.IngestionJob))).scalar_one()
    assert jobs == 1, "copies share the original's index"


async def test_chat_job_is_hidden_from_the_knowledge_api_even_without_a_base(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
        job = await _job_for(app_session, tenant, aid)
        async with app_session(tenant) as db:
            (await db.get(m.IngestionJob, job.id)).kb_id = None
        r = await c.get(f"/api/v1/knowledge/jobs/{job.id}", headers=_token(tenant))
        assert r.status_code == 404, r.text


async def test_chat_kb_for_reraises_an_unrelated_integrity_error(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member = m.OrgMember(tenant_id=tenant, subject="op", subject_uuid=uuid.uuid4())
        db.add(member)
        await db.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=member.id)
        db.add(session)
        await db.flush()

        async def broken_flush(*args: Any, **kwargs: Any) -> None:
            raise IntegrityError("INSERT", {}, Exception('violates "ck_something_else"'))

        monkeypatch.setattr(db, "flush", broken_flush)
        with pytest.raises(IntegrityError):
            await chat_kb_for(db, session=session, create=True)
        monkeypatch.undo()


async def test_a_queue_that_cannot_take_the_job_falls_back_inline(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    router: _FakeEmbedRouter,
    queue: _RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unreachable(**_: object) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(queue, "enqueue", unreachable)
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
    assert (await _attachment(app_session, tenant, aid)).index_status == "failed"
    assert (await _job_for(app_session, tenant, aid)).status == "failed"


async def _indexed_upload(
    app_session: AppSessionFactory, c: Any, tenant: uuid.UUID, sid: str, name: str
) -> str:
    aid = await _upload(c, tenant, sid, name, _text(INLINE_ATTACHMENT_CHARS + 5_000))
    await ingest_job(_message(await _job_for(app_session, tenant, aid)))
    assert (await _attachment(app_session, tenant, aid)).index_status == "indexed"
    return aid


async def test_deleting_a_chat_file_erases_its_chunks(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        kept = await _indexed_upload(app_session, c, tenant, sid, "kept.txt")
        gone = await _indexed_upload(app_session, c, tenant, sid, "gone.txt")
        r = await c.delete(f"/api/v1/files/{gone}", headers=_token(tenant))
        assert r.status_code == 204, r.text
    live = await _live_chunks(app_session, tenant)
    assert live and {ch.chunk_metadata["attachment_id"] for ch in live} == {kept}
    async with app_session(tenant) as db:
        erased = (
            await db.execute(
                select(func.count())
                .select_from(m.KbChunk)
                .where(m.KbChunk.source_uri == f"chat-file://{gone}", m.KbChunk.content == "")
            )
        ).scalar_one()
    assert erased > 0, "reduced, not merely hidden"


async def test_deleting_a_queued_file_fails_its_ingest(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
        job = await _job_for(app_session, tenant, aid)
        r = await c.delete(f"/api/v1/files/{aid}", headers=_token(tenant))
        assert r.status_code == 204, r.text
    assert (await _job_for(app_session, tenant, aid)).status == "failed"
    await ingest_job(_message(job))
    assert await _live_chunks(app_session, tenant) == []


async def test_an_index_shared_with_a_copy_stays_until_the_last_file_goes(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        original = await _indexed_upload(app_session, c, tenant, sid, "report.txt")
        async with app_session(tenant) as db:
            row = await db.get(m.FileAttachment, uuid.UUID(original))
            copy = await copy_attachment(db, row, owner_id=uuid.UUID(sid))
            copy_id = str(copy.id)
        r = await c.delete(f"/api/v1/files/{original}", headers=_token(tenant))
        assert r.status_code == 204, r.text
        assert await _live_chunks(app_session, tenant), "the copy still uses the index"
        async with app_session(tenant) as db:
            copied = await db.get(m.FileAttachment, uuid.UUID(copy_id))
            assert copied.index_status == "indexed"
            again = await copy_attachment(db, copied, owner_id=uuid.UUID(sid))
            assert again.index_status == "failed", "a copy of a copy whose original is gone"
        r = await c.delete(f"/api/v1/files/{copy_id}", headers=_token(tenant))
        assert r.status_code == 204, r.text
    assert await _live_chunks(app_session, tenant) == []


async def test_a_copy_whose_original_is_gone_falls_back_inline(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        original = await _upload(c, tenant, sid, "a.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
        async with app_session(tenant) as db:
            row = await db.get(m.FileAttachment, uuid.UUID(original))
            copy = await copy_attachment(db, row, owner_id=uuid.UUID(sid))
            copy_id = copy.id
        r = await c.delete(f"/api/v1/files/{original}", headers=_token(tenant))
        assert r.status_code == 204, r.text
    async with app_session(tenant) as db:
        copied = await db.get(m.FileAttachment, copy_id)
        late = await copy_attachment(db, copied, owner_id=uuid.UUID(sid))
        assert late.index_status == "failed" and late.indexed_from_id is None


class _RecordingRouter(_FakeEmbedRouter):
    def __init__(self) -> None:
        self.require_local: list[object] = []

    async def embed(self, text: str, model: str | None = None, **kw: object) -> list[float]:
        self.require_local.append(kw.get("require_local"))
        return await super().embed(text, model)


async def _agent_on(app_session: AppSessionFactory, tenant: uuid.UUID, provider: str) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="A", presentation={"provider": provider}
        )
        db.add(agent)
        await db.flush()
        return agent.id


@pytest.mark.parametrize(("provider", "local"), [("ollama", True), ("anthropic", False)])
async def test_a_local_agents_chat_files_are_embedded_locally_only(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    local: bool,
) -> None:
    recording = _RecordingRouter()
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: recording)
    tenant = uuid.uuid4()
    agent_id = await _agent_on(app_session, tenant, provider)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "notes.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
    job = await _job_for(app_session, tenant, aid)
    await ingest_job(_message(job))
    assert recording.require_local, "the file was embedded"
    # Every call told the router to refuse any cloud model, or none did.
    assert set(recording.require_local) == {LOCAL_AGENT_REASON if local else None}


async def test_a_local_agents_file_goes_inline_when_only_cloud_embeddings_exist(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _CloudOnlyRouter())
    tenant = uuid.uuid4()
    agent_id = await _agent_on(app_session, tenant, "ollama")
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "notes.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
    await ingest_job(_message(await _job_for(app_session, tenant, aid)))
    row = await _attachment(app_session, tenant, aid)
    assert row.index_status == "failed"
    assert await _live_chunks(app_session, tenant) == []
    assert "read_chat_file" not in attachment_turn_text(row)


async def test_a_base_made_before_the_agent_went_local_still_embeds_locally(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _RecordingRouter()
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: recording)
    tenant = uuid.uuid4()
    agent_id = await _agent_on(app_session, tenant, "anthropic")
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "notes.txt", _text(INLINE_ATTACHMENT_CHARS + 5))
    async with app_session(tenant) as db:
        (await db.get(m.Agent, agent_id)).presentation = {"provider": "ollama"}
    await ingest_job(_message(await _job_for(app_session, tenant, aid)))
    assert recording.require_local
    assert set(recording.require_local) == {LOCAL_AGENT_REASON}
