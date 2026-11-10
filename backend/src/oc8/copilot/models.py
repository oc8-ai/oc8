"""Persistent, deliberately secret-free Copilot proposal records."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class CopilotProposal(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "copilot_proposal"

    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    applied_by: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, default=uuid.uuid4)
    #: "operations": a reviewed list of changes (the admin proposal API).
    #: "agent_builder": one new agent with its settings, proposed in a chat
    #: (`oc8.copilot.builder`), whose later operations name the agent by `agentRef`.
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="operations", server_default="operations"
    )
    #: The chat a builder proposal belongs to; NULL for "operations".
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: A builder proposal expires after seven days (spec 3.5).
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: What happened to a builder proposal, for its card: `agentId` once
    #: applied, `approvalId` once requested, `refusal` + `failedStep` when apply
    #: was refused, `rejectedReason` when an approver said no. Codes and ids only.
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','applied','expired','rejected','awaiting_approval','superseded')",
            name="ck_copilot_proposal_status",
        ),
        CheckConstraint("kind IN ('operations','agent_builder')", name="ck_copilot_proposal_kind"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_copilot_proposal_idempotency"),
        Index("ix_copilot_proposal_session_status", "session_id", "status"),
        Index(
            "uq_copilot_proposal_open_builder",
            "session_id",
            unique=True,
            postgresql_where=text(
                "kind = 'agent_builder' AND status IN ('draft','awaiting_approval')"
            ),
        ),
    )

    def as_json(self) -> dict[str, Any]:
        """Review representation: labels/references only, never operation data."""
        return {
            "id": str(self.id),
            "status": self.status,
            "revision": self.revision,
            "operations": [],
        }


class CopilotOperation(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "copilot_operation"

    proposal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Validated typed values only. Written by capabilities.py and, for builder
    # proposals, by builder_ops.builder_operation_data -- never an arbitrary
    # request payload.
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target_revision: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("proposal_id", "ordinal", name="uq_copilot_operation_order"),
    )
