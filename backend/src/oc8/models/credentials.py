"""The `credential` table (unified credentials framework design, §1).

A named, typed SHELL around values that live in the existing `secret_store`
(oc8.secrets.service) -- this model adds no new encryption path. Every
secret-kind field's plaintext lives under a generated secret_store name
(`credential/{credential.id}/{field_key}`), referenced here by
`secret_refs`. `field_values` holds only the NON-secret fields (e.g. an S3
credential's `region`) -- secret values are never persisted on this row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Uuid, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class Credential(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "credential"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    credential_type: Mapped[str] = mapped_column(Text, nullable=False)
    field_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secret_refs: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)

    #: NULL: the organisation's credential (every row before migration 0107).
    #: Otherwise one member's own login ("Nur für mich").
    owner_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)

    #: The organisation's login is enabled for every department (a personal
    #: login, `owner_member_id` set, never is).
    all_departments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    #: Ids of the former `mcp_connection` rows folded into this login (migration
    #: of old connections); lets that fold run once.
    consolidated_from: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_member_id",
            "name",
            name="uq_credential_tenant_owner_name",
            postgresql_nulls_not_distinct=True,
        ),
    )


class CredentialDepartment(Base, PkMixin, TenantMixin):
    """One organisation login enabled for one department."""

    __tablename__ = "credential_department"

    credential_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("credential.id", ondelete="CASCADE"), nullable=False
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("department.id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("credential_id", "department_id", name="uq_credential_department"),
    )
