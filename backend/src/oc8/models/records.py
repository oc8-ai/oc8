"""The tenant's registry of record links oc8 issued into tool results."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, DateTime, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base
from oc8.models._mixins import PkMixin, TenantMixin


class RecordLink(Base, PkMixin, TenantMixin):
    __tablename__ = "record_link"

    url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    #: `registry.canonical(url)`: where the link leads, however it is spelled.
    canon: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "url", name="uq_record_link_tenant_url"),
        CheckConstraint("char_length(url) <= 2000", name="ck_record_link_url"),
        Index("ix_record_link_tenant_host", "tenant_id", "host"),
        Index("ix_record_link_tenant_canon", "tenant_id", "canon"),
    )
