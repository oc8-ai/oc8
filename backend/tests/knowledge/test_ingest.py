from __future__ import annotations

import base64
import io
import uuid
from typing import Any

import docx
import openpyxl
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.knowledge.ingest import (
    MAX_DOCUMENT_LENGTH,
    IngestionError,
    KnowledgeBaseNotFoundError,
    chunk_text,
    extract_text,
    ingest_document,
)
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdfReader:
    def __init__(self, _stream: Any) -> None:
        self.pages = [_FakePage("page one text"), _FakePage("page two text")]


def test_chunk_text_empty_input_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_chunk_text_shorter_than_chunk_size_yields_single_chunk() -> None:
    assert chunk_text("short text", chunk_size=1000, overlap=100) == ["short text"]


def test_chunk_text_produces_overlapping_chunks() -> None:
    text = "x" * 2500
    chunks = chunk_text(text, chunk_size=1000, overlap=100)
    assert len(chunks) == 3
    assert len(chunks[0]) == 1000
    assert chunks[0][-100:] == chunks[1][:100]


def test_extract_text_plain() -> None:
    assert extract_text(content="  hello world  ", content_type="text/plain") == "hello world"


def test_extract_text_markdown() -> None:
    assert (
        extract_text(content="# Title\n\nBody", content_type="text/markdown")
        == "# Title\n\nBody"
    )


def test_extract_text_pdf_uses_pypdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.PdfReader", _FakePdfReader)
    encoded = base64.b64encode(b"fake pdf bytes").decode()
    result = extract_text(content=encoded, content_type="application/pdf")
    assert result == "page one text\n\npage two text"


def test_extract_text_rejects_unsupported_content_type() -> None:
    with pytest.raises(IngestionError):
        extract_text(content="x", content_type="application/octet-stream")


def test_extract_text_raises_on_corrupted_pdf() -> None:
    # Genuinely invalid PDF bytes fed to the real, unmocked pypdf.PdfReader —
    # tests our error handling against a real parse failure, not pypdf's
    # own correctness.
    encoded = base64.b64encode(b"this is not a valid pdf file at all").decode()
    with pytest.raises(IngestionError):
        extract_text(content=encoded, content_type="application/pdf")


async def test_ingest_document_raises_knowledge_base_not_found_error(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        with pytest.raises(KnowledgeBaseNotFoundError):
            await ingest_document(
                db,
                tenant_id=tenant,
                kb_id=uuid.uuid4(),
                filename="x.txt",
                content="x",
                content_type="text/plain",
            )


async def test_ingest_document_text_end_to_end(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Docs", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()

        job = await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="notes.txt",
            content="word " * 400,  # long enough to span multiple chunks
            content_type="text/plain",
        )
        assert job.status == "succeeded"
        assert job.stats["chunks"] > 1

        chunks = (
            (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id)))
            .scalars()
            .all()
        )
        assert len(chunks) == job.stats["chunks"]
        assert all(c.embedding is not None for c in chunks)
        assert kb.freshness["docs"] == 1
        assert kb.freshness["chunks"] == len(chunks)


async def test_ingest_document_pdf_end_to_end(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.ingest.PdfReader", _FakePdfReader)
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Docs2", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()

        encoded = base64.b64encode(b"fake pdf bytes").decode()
        job = await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="report.pdf",
            content=encoded,
            content_type="application/pdf",
        )
        assert job.status == "succeeded"


async def test_ingest_document_failure_persists_failed_job(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Docs3", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()

        with pytest.raises(IngestionError):
            await ingest_document(
                db,
                tenant_id=tenant,
                kb_id=kb.id,
                filename="bad.bin",
                content="x",
                content_type="application/octet-stream",
            )

        jobs = (
            (await db.execute(select(m.IngestionJob).where(m.IngestionJob.kb_id == kb.id)))
            .scalars()
            .all()
        )
        assert len(jobs) == 1
        assert jobs[0].status == "failed"


async def test_ingest_document_rejects_oversized_content(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(tenant_id=tenant, name="Docs4", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()

        with pytest.raises(IngestionError):
            await ingest_document(
                db,
                tenant_id=tenant,
                kb_id=kb.id,
                filename="huge.txt",
                content="x" * (MAX_DOCUMENT_LENGTH + 1),
                content_type="text/plain",
            )

        jobs = (
            (await db.execute(select(m.IngestionJob).where(m.IngestionJob.kb_id == kb.id)))
            .scalars()
            .all()
        )
        assert len(jobs) == 1
        assert jobs[0].status == "failed"


def _docx_bytes(paragraphs: list[str]) -> bytes:
    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_extract_text_handles_docx() -> None:
    raw = _docx_bytes(["First paragraph.", "Second paragraph."])
    content = base64.b64encode(raw).decode()
    result = extract_text(
        content=content,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "First paragraph." in result
    assert "Second paragraph." in result


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_extract_text_handles_xlsx() -> None:
    raw = _xlsx_bytes([["Name", "Amount"], ["Acme", "100"]])
    content = base64.b64encode(raw).decode()
    result = extract_text(
        content=content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert "Acme" in result
    assert "100" in result


def test_extract_text_handles_csv() -> None:
    result = extract_text(content="Name,Amount\nAcme,100\n", content_type="text/csv")
    assert "Acme" in result
    assert "100" in result


def test_extract_text_still_refuses_images() -> None:
    with pytest.raises(IngestionError, match="unsupported content_type"):
        extract_text(content="", content_type="image/png")
