"""Files attached to a chat turn, an agent's standing Instructions, or
produced by an agent run. Bytes live in the object store (oc8.storage.s3)
keyed by `bucket_key`; this row is metadata + (for text-extractable types)
the extracted text, never the raw bytes themselves."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class FileAttachment(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "file_attachment"

    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    bucket_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    is_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: A chat file's indexing (spec §5.4, migration 0138): None (not a chat
    #: file, or older than this), 'inline' (small enough to hand over whole),
    #: 'queued', 'indexed' (searchable in the chat's own knowledge base) or
    #: 'failed' (handed over inline and truncated instead).
    index_status: Mapped[str | None] = mapped_column(Text)
    #: The attachment whose indexed text this row shares -- an edited turn's
    #: copy of a file is not ingested again. None for the original itself.
    indexed_from_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        CheckConstraint(
            "owner_type IN ('chat_message','agent_instructions','agent_run')",
            name="ck_file_attachment_owner_type",
        ),
        CheckConstraint(
            "index_status IS NULL OR index_status IN ('queued','indexed','failed','inline')",
            name="ck_file_attachment_index_status",
        ),
    )
