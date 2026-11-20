"""Web Push subscriptions -- one row per browser/device an operator has
opted into push notifications on."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class PushSubscription(Base, PkMixin, TenantMixin, TimestampMixin):
    """A Web Push subscription for one operator's browser/device.

    `(tenant_id, endpoint)` is unique, and `endpoint` alone is NOT. The push
    service assigns one endpoint per browser installation against this
    instance's single VAPID key pair -- it knows nothing about tenants -- so
    the same browser presents the SAME endpoint under every tenant its user
    belongs to. A global unique constraint would make the second tenant's
    subscribe an IntegrityError that no tenant-scoped SELECT could have seen
    coming, because RLS hides the other tenant's row from the lookup that is
    supposed to detect it.

    Scoped per tenant it is still the natural upsert key: re-subscribing the
    same browser under the same tenant (e.g. after toggling off and back on)
    updates the existing row instead of creating a duplicate.
    """

    __tablename__ = "push_subscription"

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("org_member.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "endpoint", name="uq_push_subscription_endpoint"),
    )


class MemberNotification(Base, PkMixin, TenantMixin):
    """One entry in a member's inbox.

    At most one UNREAD row exists per `(member_id, kind, subject_id)`: the
    partial unique index is NULLS NOT DISTINCT, so a NULL `subject_id` collapses
    too. `title`/`body` are `{"en": ..., "de": ...}`."""

    __tablename__ = "member_notification"

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("org_member.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    title: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    link: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_member_notification_member_created", "member_id", text("created_at DESC")),
        Index(
            "uq_member_notification_unread",
            "member_id",
            "kind",
            "subject_id",
            unique=True,
            postgresql_where=text("read_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
    )


class MemberNotificationSetting(Base, PkMixin, TenantMixin):
    """A member's per-kind channel opt-outs (absent row = both enabled)."""

    __tablename__ = "member_notification_setting"

    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("org_member.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    push_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    messenger_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (UniqueConstraint("member_id", "kind", name="uq_member_notification_setting"),)
