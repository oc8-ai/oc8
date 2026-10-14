"""Trigger Service (§8.4): cron-schedule, source-specific event, and generic
webhook trigger rows."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class Trigger(Base, PkMixin, TenantMixin, TimestampMixin):
    """Discriminated union: kind='cron' rows carry cron_expression and
    nothing else; kind='event' rows carry event_source/event_type (a signed,
    source-specific push -- GitHub today) and no cron_expression/
    webhook_token; kind='webhook' rows carry webhook_token (a generic,
    n8n-Webhook-node-style trigger -- ANY caller that can POST JSON to its
    one unguessable URL fires it, no per-source adapter code, no signature);
    kind='once' rows carry next_run_at only (a Copilot follow-up's single instant).
    Enforced by ck_trigger_kind_fields below."""

    __tablename__ = "trigger"

    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)

    cron_expression: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    event_source: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str | None] = mapped_column(Text)

    # The entire auth boundary for a kind='webhook' row: a high-entropy
    # (32-byte urlsafe, ~256 bits) random token, unique across every tenant
    # so POST /webhooks/{token} can look a row up with no tenant context yet
    # (mirrors handle_inbound_event's own all-tenant discovery loop, since
    # RLS means no unbound query can filter by tenant first).
    webhook_token: Mapped[str | None] = mapped_column(Text, unique=True)

    #: Set only on a Copilot follow-up (design §7a.4): the member's conversation
    #: it fires into, and the responsibility it serves.
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    responsibility_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    #: IANA zone the cron expression is read in. Required for a follow-up.
    timezone: Mapped[str | None] = mapped_column(Text)
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why the last due fire did not run ("paused", "busy", ...); None after a
    #: real fire. Lets a resume catch up exactly what the pause skipped.
    last_skip_reason: Mapped[str | None] = mapped_column(Text)
    #: Copilot follow-ups only: "check_in" or "research" (NULL == check_in).
    followup_purpose: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("kind IN ('cron','event','webhook','once')", name="ck_trigger_kind"),
        CheckConstraint(
            "(kind = 'cron' AND cron_expression IS NOT NULL "
            "AND event_source IS NULL AND event_type IS NULL AND webhook_token IS NULL) "
            "OR (kind = 'event' AND event_source IS NOT NULL AND event_type IS NOT NULL "
            "AND cron_expression IS NULL AND webhook_token IS NULL) "
            "OR (kind = 'webhook' AND webhook_token IS NOT NULL "
            "AND cron_expression IS NULL AND event_source IS NULL AND event_type IS NULL) "
            "OR (kind = 'once' AND next_run_at IS NOT NULL AND cron_expression IS NULL "
            "AND event_source IS NULL AND event_type IS NULL AND webhook_token IS NULL)",
            name="ck_trigger_kind_fields",
        ),
        CheckConstraint(
            "chat_session_id IS NULL OR (responsibility_id IS NOT NULL AND timezone IS NOT NULL "
            "AND (kind <> 'cron' OR ends_at IS NOT NULL) AND kind IN ('cron','once'))",
            name="ck_trigger_followup_fields",
        ),
    )
