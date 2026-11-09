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

from sqlalchemy import Boolean, DateTime, Text, UniqueConstraint, Uuid
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

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_member_id",
            "name",
            name="uq_credential_tenant_owner_name",
            postgresql_nulls_not_distinct=True,
        ),
    )
