"""The Copilot's per-member layer (design §7a): one Copilot agent, a profile
and responsibilities per member. Nothing here grants authority -- every turn
still resolves the member through the chat session behind it."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin

RESPONSIBILITY_STATES = ("active", "waiting", "paused", "done", "cancelled")
NOTIFY_RULES = ("decisions_only", "risks_and_decisions", "every_update")


class CopilotProfile(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "copilot_profile"

    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    display_name: Mapped[str] = mapped_column(
        Text, nullable=False, default="Copilot", server_default="Copilot"
    )
    avatar: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"shape": "round", "color": "indigo"},
        server_default=text("""'{"shape": "round", "color": "indigo"}'::jsonb"""),
    )
    paused_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_id", name="uq_copilot_profile_member"
        ),
        CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 40",
            name="ck_copilot_profile_name",
        ),
    )


class Responsibility(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "responsibility"

    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    chat_session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    next_step: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    notify_rule: Mapped[str] = mapped_column(
        Text, nullable=False, default="risks_and_decisions", server_default="risks_and_decisions"
    )
    #: The messenger plugin id this was opened from, or None for web (§7a.3).
    origin_channel: Mapped[str | None] = mapped_column(Text)
    last_update_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: The follow-up run that last reported under notify_rule; a follow-up run
    #: that ends without being this id stays silent (§7a.3).
    last_report_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "state IN ('active','waiting','paused','done','cancelled')",
            name="ck_responsibility_state",
        ),
        CheckConstraint(
            "notify_rule IN ('decisions_only','risks_and_decisions','every_update')",
            name="ck_responsibility_notify_rule",
        ),
        CheckConstraint("char_length(goal) > 0", name="ck_responsibility_goal"),
        Index("ix_responsibility_member_state", "tenant_id", "member_id", "state"),
    )
