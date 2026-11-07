"""A member's own guardrails on one agent (D3a). Same shape as
`Agent.narrowing`; evaluated as the third, tighten-only PDP term."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class MemberGuardrail(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "member_guardrail"
    __table_args__ = (
        UniqueConstraint("tenant_id", "member_id", "agent_id", name="uq_member_guardrail"),
    )

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("org_member.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Bumped by the database trigger on every update (see Agent.config_revision).
    config_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
