"""Data boards: a stored view (the spec) plus its own append-only data store.

A producing agent appends batches of records on each run; history is kept
("as reported then") and never updated in place -- the app role has no UPDATE
grant on batches or records (migration 0122).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, SoftDeleteMixin, TenantMixin


class DataBoard(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """`deleted_at` = archived with its department (restorable); `removed_at` =
    a person deleted it (records purged, the row kept as a tombstone)."""

    __tablename__ = "data_board"

    department_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("department.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    removed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_data_board_department", "tenant_id", "department_id"),
        Index("ix_data_board_agent", "agent_id"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_data_board_title"),
    )


class DataBoardSpecVersion(Base, PkMixin, TenantMixin):
    __tablename__ = "data_board_spec_version"

    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("data_board.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("board_id", "version", name="uq_data_board_spec_version"),)


class DataBoardBatch(Base, PkMixin, TenantMixin):
    """One write of rows for one dataset at one `as_of`. Append-only."""

    __tablename__ = "data_board_batch"

    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("data_board.id", ondelete="CASCADE"), nullable=False
    )
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Provenance only -- deliberately no FK, so purging runs never touches history.
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ix_data_board_batch_period",
            "board_id",
            "dataset",
            "as_of",
            text("recorded_at DESC"),
        ),
        CheckConstraint("row_count >= 1", name="ck_data_board_batch_row_count"),
        CheckConstraint("char_length(note) <= 2000", name="ck_data_board_batch_note"),
    )


class DataBoardRecord(Base, PkMixin, TenantMixin):
    """One row of a batch. Append-only: never updated in place."""

    __tablename__ = "data_board_record"

    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("data_board.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("data_board_batch.id", ondelete="CASCADE"), nullable=False
    )
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_data_board_record_period", "board_id", "dataset", "as_of"),
        Index("ix_data_board_record_batch", "batch_id"),
    )


class DataBoardShare(Base, PkMixin, TenantMixin):
    """Read access to one board for one member OR one department (exactly one).

    Grants nothing beyond reading and querying that board."""

    __tablename__ = "data_board_share"

    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("data_board.id", ondelete="CASCADE"), nullable=False
    )
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("org_member.id", ondelete="CASCADE"), nullable=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("department.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(member_id IS NULL) <> (department_id IS NULL)",
            name="ck_data_board_share_one_target",
        ),
        UniqueConstraint("board_id", "member_id", name="uq_data_board_share_member"),
        UniqueConstraint("board_id", "department_id", name="uq_data_board_share_department"),
        Index("ix_data_board_share_member", "member_id"),
        Index("ix_data_board_share_department", "department_id"),
    )
