"""Shared `FileAttachment` write path: S3 + text extraction, independent of
how the bytes arrived. `api/v1/files.py`'s upload endpoints and the agent
layer (`agent/control_tools.py`'s `write_output_file`, the run-output sync
in `runtime/executor.py`) both funnel through `store_attachment_bytes` so
neither has to duplicate the size cap, content-type allowlist, or the
extraction/truncation rules. The agent layer must not import the API layer,
so this lives in `storage`, not `api/v1/files.py`.
"""

from __future__ import annotations

import base64
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.knowledge.ingest import MAX_DOCUMENT_LENGTH, IngestionError, extract_text
from oc8.storage import s3

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "text/plain",
    "text/markdown",
    "text/html",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}
IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_BINARY_EXTRACT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class AttachmentRejected(Exception):
    """Base for the two rejection reasons callers must distinguish (413 vs 422
    at the API layer; a plain tool error for the agent layer)."""


class UnsupportedContentType(AttachmentRejected):
    pass


class AttachmentTooLarge(AttachmentRejected):
    pass


async def store_attachment_bytes(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_type: str,
    owner_id: uuid.UUID,
    filename: str,
    raw: bytes,
    content_type: str,
) -> m.FileAttachment:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UnsupportedContentType(f"unsupported content type: {content_type!r}")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise AttachmentTooLarge("file exceeds 25 MB limit")

    is_image = content_type in IMAGE_CONTENT_TYPES
    extracted_text: str | None = None
    if not is_image:
        payload = (
            base64.b64encode(raw).decode()
            if content_type in _BINARY_EXTRACT_TYPES
            else raw.decode("utf-8", errors="replace")
        )
        try:
            extracted_text = extract_text(content=payload, content_type=content_type)
        except IngestionError:
            extracted_text = None  # storage still succeeds -- see files.py's Global Constraints
        if extracted_text is not None and len(extracted_text) > MAX_DOCUMENT_LENGTH:
            # Truncate, never refuse -- the same 200,000-char cap the
            # knowledge-base ingest path uses, applied here because
            # `extract_text` itself does not enforce it (its KB caller does,
            # by failing the job -- not an option here, where an
            # oversized-but-readable file must still store successfully).
            # Without this, a 25 MB spreadsheet's whole extracted text is
            # appended verbatim to a run's task text and sent to the model:
            # nothing downstream bounds it, since `trim_to_budget` always
            # keeps the newest message even when it alone blows the budget.
            extracted_text = extracted_text[:MAX_DOCUMENT_LENGTH]

    bucket_key = f"{tenant_id}/{owner_type}/{uuid.uuid4()}-{filename}"
    await s3.put_object(bucket_key, raw, content_type)

    row = m.FileAttachment(
        tenant_id=tenant_id,
        owner_type=owner_type,
        owner_id=owner_id,
        bucket_key=bucket_key,
        filename=filename,
        content_type=content_type,
        size_bytes=len(raw),
        extracted_text=extracted_text,
        is_image=is_image,
    )
    db.add(row)
    await db.flush()
    return row
