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

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class ChatSession(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "chat_session"
    __table_args__ = (
        CheckConstraint("kind IN ('chat','live')", name="ck_chat_session_kind"),
        Index(
            "uq_chat_session_live",
            "tenant_id",
            "member_id",
            "agent_id",
            unique=True,
            postgresql_where=text("kind = 'live'"),
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    last_message_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # The one Task every turn in this session shares (opened by the first
    # message) -- so a chat session is one item on the department board, not
    # a new one per message.
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: The model this chat runs on instead of the agent's pinned one. NULL is
    #: "Auto" -- the agent's own model. Validated on write AND re-checked by
    #: `oc8.chat.model_choice.apply_model_override` every time a run resolves
    #: its version, so a model later removed from the set of chat-enabled
    #: models stops being used without anybody editing the chat.
    model_override_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: "chat" for an ordinary conversation, "live" for the one continuous
    #: Live timeline per (member, agent) (spec §3.1); `uq_chat_session_live`
    #: (migration 0108) keeps that to one row.
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="chat", server_default="chat")
    #: Context pinned to this chat (spec §5.2): `[{"kind","id","label"}]`, each
    #: authorised against the member when pinned and re-checked per reader.
    pinned_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    #: Kept at the top of the history list.
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


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
    #: The member who wrote a human turn. NULL for assistant turns and for
    #: rows written before 0107. Prepares for Rooms (spec §7).
    author_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: For an assistant turn: the user turn it answers. Two answers to the same
    #: turn (Regenerate) share it.
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    #: Typed message parts (sources, answers, document refs). NULL for rows
    #: written before 0107, which render from `content`.
    parts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','followup')", name="ck_chat_message_role"),
    )


class ChatProject(Base, PkMixin, TenantMixin, TimestampMixin):
    """A folder of chats with shared instructions and pinned context (spec §5.2).
    Each chat in it stays private to its member."""

    __tablename__ = "chat_project"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 120", name="ck_chat_project_name"),
        CheckConstraint("char_length(instructions) <= 8000", name="ck_chat_project_instructions"),
        Index("ix_chat_project_tenant_owner", "tenant_id", "owner_member_id"),
    )

    owner_member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    pinned_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: Reserved for the later sharing work; never read in core.
    shared_with: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
