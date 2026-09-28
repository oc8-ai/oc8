"""ORM models for supervision policies, assignments, checkpoints, and interventions."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Numeric, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class SupervisionPolicy(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "supervision_policy"

    department_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    checkpoint_every: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    drift_thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    allowed_interventions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    judge_model_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sampling_rate: Mapped[float] = mapped_column(Numeric, nullable=False, default=1.0)
    judge_rubric: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SupervisionAssignment(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "supervision_assignment"

    supervisor_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    supervised_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (UniqueConstraint("supervised_agent_id", name="uq_supervision_supervised"),)


class TaskAnchor(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "task_anchor"

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    anchor_embedding: Mapped[list[Any] | None] = mapped_column(JSONB)

    __table_args__ = (UniqueConstraint("task_id", name="uq_task_anchor_task"),)


class AgentCheckpoint(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "agent_checkpoint"

    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    seq: Mapped[int] = mapped_column(nullable=False)
    state_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    context_ref: Mapped[str | None] = mapped_column(Text)
    drift_score: Mapped[float | None] = mapped_column(Numeric)
    verdict: Mapped[str | None] = mapped_column(Text)
    judge_verdict: Mapped[str | None] = mapped_column(Text)
    judge_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("agent_id", "task_id", "seq", name="uq_agent_checkpoint_seq"),
        CheckConstraint(
            "verdict IS NULL OR verdict IN ('on_track','drifting','off_track')",
            name="ck_agent_checkpoint_verdict",
        ),
        CheckConstraint(
            "judge_verdict IS NULL OR judge_verdict IN ('on_track','drifting','off_track')",
            name="ck_agent_checkpoint_judge_verdict",
        ),
    )


class SupervisionIntervention(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "supervision_intervention"

    supervisor_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    supervised_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    outcome: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('steer','rewind','pause_escalate','reassign')",
            name="ck_supervision_intervention_kind",
        ),
    )
