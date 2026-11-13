"""The tenant's registry of record links oc8 issued into tool results."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base
from oc8.models._mixins import PkMixin, TenantMixin


class RecordLink(Base, PkMixin, TenantMixin):
    __tablename__ = "record_link"

    url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("tenant_id", "url", name="uq_record_link_tenant_url"),)
