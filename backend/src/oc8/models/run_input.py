"""A message somebody wrote to a RUNNING run (spec §3.2).

Consumed exactly once, in order, at a step boundary: the in-process engine
appends it as a user message, an external runtime finds it on its next MCP tool
result. `consumed_step` is what the Live tab shows ("Taken into account in step
N")."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class RunInput(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "run_input"
    __table_args__ = (
        CheckConstraint("char_length(text) BETWEEN 1 AND 4000", name="ck_run_input_text"),
        Index("ix_run_input_run_pending", "run_id", "consumed_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: Acceptance order. Writers to one run are serialised (`add_run_input`), so
    #: a lower `seq` is always committed before a higher one; `created_at` is
    #: the writer's transaction START and cannot order them.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    author_member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_step: Mapped[int | None] = mapped_column(Integer)
