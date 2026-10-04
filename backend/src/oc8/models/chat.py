"""Durable direct-chat sessions between an operator and one agent.

Each user message becomes a real `AgentRun` (`source="chat"`), so a chat turn
that needs an approval suspends and resumes exactly like an autonomous run --
no separate guardrail path to keep in sync. `ChatMessage` is the durable
transcript; `run_id` links an assistant message back to the run that produced
it (nullable because a user message has no run of its own)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class ChatSession(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "chat_session"

    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    last_message_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # The one Task every turn in this session shares (opened by the first
    # message) -- so a chat session is one item on the department board, not
    # a new one per message.
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class ChatMessage(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "chat_message"

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    rendered_components: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: The slash command this turn was sent in -- see `oc8.chat.modes`. NULL
    #: for an ordinary message, which is most of them. Stored rather than
    #: re-derived from `content`, because the command word is stripped out of
    #: content: the transcript shows what was said, the badge shows how it was
    #: asked. No CHECK constraint on purpose -- the mode vocabulary is
    #: application-level and a newer release must not be refused by an older
    #: database.
    mode: Mapped[str | None] = mapped_column(Text)
    #: What the operator attached to THIS turn with `#`:
    #: `[{"kind": "knowledge_base", "id": "...", "label": "..."}]`. Resolved and
    #: labelled at send time, so renaming or deleting a knowledge base later
    #: leaves an old turn readable instead of showing a bare uuid.
    context_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','followup')", name="ck_chat_message_role"
        ),
    )
