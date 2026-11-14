"""A conversation's permission for the Copilot to act through one connection
(copilot direct access design §5). Reads only: every write still asks."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base
from oc8.models._mixins import PkMixin, TenantMixin


class DirectAccessGrant(Base, PkMixin, TenantMixin):
    __tablename__ = "direct_access_grant"

    chat_session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False
    )
    #: The person who approved it -- always the conversation's own member.
    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    tool_key: Mapped[str] = mapped_column(Text, nullable=False)
    approval_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("approval_request.id", ondelete="CASCADE"), nullable=False
    )
    granted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Calls executed through this grant (spec §11: the agent suggestion
    #: after the second direct action).
    actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Set on the grant whose call crossed the threshold: the suggestion is
    #: made once per conversation.
    agent_suggested_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_direct_access_grant_active",
            "tenant_id",
            "chat_session_id",
            "tool_key",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_direct_access_grant_tenant_member", "tenant_id", "member_id"),
        CheckConstraint(
            "char_length(tool_key) BETWEEN 1 AND 200", name="ck_direct_access_grant_tool_key"
        ),
    )
