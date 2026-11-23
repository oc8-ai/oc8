"""Trigger Service (§8.4): cron-schedule, source-specific event, and generic
webhook trigger rows."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
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
    #: The member who created (or last re-saved) this trigger: whose own
    #: account a tool runs through when the agent's access is "in the user's
    #: context". NULL for triggers made before migration 0107.
    created_by_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    #: kind='webhook'/'event' only: the creator chose to let this trigger act
    #: through their account (anyone who can reach the URL fires it, so this is
    #: never the default). A schedule needs no such flag. Reset when someone
    #: else edits the trigger.
    acts_for_creator: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    #: The agent's access fingerprint (connections.access.trigger_fingerprint)
    #: when the creator last created or saved this trigger. Its runs act
    #: through the creator's own account only while it still matches; NULL
    #: (saved before migration 0110) never does.
    access_fingerprint: Mapped[str | None] = mapped_column(Text)
    #: kind='event' on a polled event only (`oc8.events.sources`): the
    #: organisation login the event is polled from. It is always the login the
    #: agent is fixed to for the event capa's tools, so the run's tools reach
    #: the same account; the trigger is switched off when that changes. A
    #: deleted login leaves it NULL (ON DELETE SET NULL) and the tick switches
    #: the trigger off.
    event_target_credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    #: The same for a login that is an OAuth connection (an app registration,
    #: a service account): a trigger names at most one of the two, as
    #: `AgentToolAccess` holds either kind (`oc8.events.sources.LoginTarget`).
    event_target_oauth_connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    #: The poller's settings for this trigger, handed to it unread by core.
    event_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

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
        CheckConstraint(
            "NOT (event_target_credential_id IS NOT NULL "
            "AND event_target_oauth_connection_id IS NOT NULL)",
            name="ck_trigger_event_target_one",
        ),
        # One polled trigger per agent and event (oc8.events.sources).
        Index(
            "uq_trigger_polled_event",
            "agent_id",
            "event_source",
            "event_type",
            unique=True,
            postgresql_where=text(
                "event_target_credential_id IS NOT NULL "
                "OR event_target_oauth_connection_id IS NOT NULL"
            ),
        ),
    )


class EventSourceCursor(Base, PkMixin, TenantMixin, TimestampMixin):
    """Where a polled trigger's poller left off (`oc8.events.sources`), one row
    per trigger. `cursor` is the poller's own opaque state; NULL means "take a
    baseline": the next poll records where the source is now and fires
    nothing, so a backlog is never caught up. Reset on creating and on
    re-enabling the trigger.

    The poll outcome lives here too, shown on the trigger: `last_error` is the
    exception class and a short message with the login's secrets removed.
    `failures` drives the back-off, `next_poll_at` when the tick may poll the
    trigger again. Never backed up (`oc8.backup.tables.EXCLUDED_TABLES`)."""

    __tablename__ = "event_source_cursor"

    trigger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    last_polled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_poll_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Drawn anew with each baseline; part of every fired event's idempotency
    #: key (`oc8.events.sources.event_key`). NULL for a baseline taken before
    #: it existed.
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: Why the source last left an event out (`PollResult.skipped`), and when.
    last_skipped: Mapped[str | None] = mapped_column(Text)
    last_skipped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("trigger_id", name="uq_event_source_cursor_trigger"),)
