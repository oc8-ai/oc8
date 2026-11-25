"""A person's standing approval of one write tool ("always allow", migration
0135). Read and written only through `oc8.approvals.allow_rules`."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base
from oc8.models._mixins import PkMixin, TenantMixin


class ToolAllowRule(Base, PkMixin, TenantMixin):
    __tablename__ = "tool_allow_rule"

    #: The person the rule is for -- and the only one it ever decides for.
    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: The connection's id, never its name; NULL for a core tool.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: The tool the held card named (`payload["tool"]`).
    tool_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_from_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        Index(
            "uq_tool_allow_rule_live",
            "tenant_id",
            "member_id",
            "agent_id",
            text("coalesce(connection_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "tool_key",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint(
            "char_length(tool_key) BETWEEN 1 AND 200", name="ck_tool_allow_rule_tool_key"
        ),
    )
