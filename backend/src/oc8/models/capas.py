"""Unified capa registry: a versioned, installable extension of any §13.2 type."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin

_CAPA_TYPES = (
    "skill, flow_template, agent_template, department_template, connector, "
    "model_adapter, runtime_adapter, tool_pack, approval_channel, core_extension"
)


class Capa(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "capa"

    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str] = mapped_column(Text, nullable=False, default="")
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="local")
    trust_level: Mapped[str] = mapped_column(Text, nullable=False, default="first_party")
    core_compat: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    config_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __table_args__ = (
        CheckConstraint(
            f"type IN ({', '.join(repr(t.strip()) for t in _CAPA_TYPES.split(','))})",
            name="ck_capa_type",
        ),
        CheckConstraint("origin IN ('local','store','custom')", name="ck_capa_origin"),
        CheckConstraint(
            "trust_level IN ('first_party','verified','community')",
            name="ck_capa_trust",
        ),
        UniqueConstraint("tenant_id", "name", name="uq_capa_tenant_name"),
    )


class CapaVersion(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "capa_version"

    capa_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    semver: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    artifact_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    entry_points: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (UniqueConstraint("capa_id", "semver", name="uq_capa_version"),)


class CapaInstallation(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "capa_installation"

    capa_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="installed")
    granted_permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(nullable=False, default=0)
    # Non-secret values a tenant submitted through the capa's setup form when
    # setup.mcp is absent (an approval_channel has no McpConnection to hold
    # them). Secrets never land here -- see configure_plugin's store_secret
    # call; this is only what channels/registry.py may read back in plain.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "status IN ('installed','enabled','disabled','quarantined')",
            name="ck_capa_installation_status",
        ),
        UniqueConstraint("tenant_id", "capa_id", name="uq_capa_installation"),
    )
