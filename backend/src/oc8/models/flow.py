"""Cross-department flows: declarative orchestration artifacts + runs (§14a.4)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, LargeBinary, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class Flow(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "flow"

    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Archived flows are hidden from the list and start no new runs; their
    # runs keep their history.
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_flow_tenant_name"),)


class FlowVersion(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "flow_version"

    flow_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    semver: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    artifact_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (UniqueConstraint("flow_id", "semver", name="uq_flow_version"),)


class FlowRun(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "flow_run"

    flow_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    current_stages: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    trigger_event: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','failed','suspended','cancelled')",
            name="ck_flow_run_status",
        ),
    )
