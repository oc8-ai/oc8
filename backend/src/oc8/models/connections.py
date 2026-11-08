"""Which account an agent uses for one tool (connections design).

One row per (agent, tool key). `user`: the account of whoever the run acts
for (chat member, starter, delegating person, trigger creator). `fixed`:
exactly one OAuth connection or credential. A fixed row whose target was
deleted has neither id; the runtime then refuses with `connection_required`
rather than picking something else. Not part of the agent's versioned
payload: an identity binding takes effect, and is revoked, at once.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class AgentToolAccess(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "agent_tool_access"

    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    tool_key: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    oauth_connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    set_by_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)

    __table_args__ = (
        UniqueConstraint("agent_id", "tool_key", name="uq_agent_tool_access"),
        CheckConstraint("mode IN ('user', 'fixed')", name="ck_agent_tool_access_mode"),
        CheckConstraint(
            "(mode = 'user' AND oauth_connection_id IS NULL AND credential_id IS NULL) "
            "OR (mode = 'fixed' "
            "AND NOT (oauth_connection_id IS NOT NULL AND credential_id IS NOT NULL))",
            name="ck_agent_tool_access_target",
        ),
    )
