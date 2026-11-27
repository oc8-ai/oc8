"""A message somebody wrote to a RUNNING run (spec §3.2).

Consumed exactly once, in order, at a step boundary: the in-process engine
appends it as a user message, an external runtime finds it on its next MCP tool
result. `consumed_step` is what the Live tab shows ("Taken into account in step
N")."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class RunInput(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "run_input"
    __table_args__ = (
        CheckConstraint("char_length(text) BETWEEN 1 AND 4000", name="ck_run_input_text"),
        CheckConstraint("delivery IN ('steer','next_turn')", name="ck_run_input_delivery"),
        Index("ix_run_input_run_pending", "run_id", "delivery", "consumed_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: Acceptance order. Writers to one run are serialised (`add_run_input`), so
    #: a lower `seq` is always committed before a higher one; `created_at` is
    #: the writer's transaction START and cannot order them.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    author_member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: `steer`: read by the run at its next step boundary. `next_turn`: never
    #: read by the run; it becomes the chat's next turn when the run ends
    #: (spec §5.5, `oc8.chat.carry_over`).
    delivery: Mapped[str] = mapped_column(
        Text, nullable=False, default="steer", server_default="steer"
    )
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_step: Mapped[int | None] = mapped_column(Integer)
    #: A `next_turn` input's "Web" switch: the turn it becomes may search the
    #: web (spec §10). Always false for `steer`: a message never changes the
    #: running run's web state.
    web: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
