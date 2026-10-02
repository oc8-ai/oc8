"""Tasks, approvals, audit, metering, activity, and integrations."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
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


class Task(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "task"

    department_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="backlog")
    meta_label: Mapped[str | None] = mapped_column(Text)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # How many delegation hops deep this task sits (§7). A root task is 0; a
    # sub-task created by delegate_task is its parent's depth + 1. Bounds the
    # delegate -> wake -> re-delegate cycle. server_default keeps every
    # pre-existing row valid.
    delegation_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        CheckConstraint(
            "state IN ('backlog','in_progress','waiting_for_approval','waiting_for_input',"
            "'done','failed','budget_exceeded')",
            name="ck_task_state",
        ),
    )


class ApprovalRequest(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "approval_request"

    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    #: Whose approval this is. NULL means TENANT-WIDE and is visible only to
    #: somebody unrestricted -- there is exactly one producer of NULL, the
    #: tenant-scope budget incident, whose subject genuinely is the company.
    #:
    #: Derived from the AGENT at raise time (`agent_id` is NOT NULL and
    #: `Agent.department_id` is NOT NULL, so there is always an answer), and
    #: denormalised rather than joined through `Agent` on purpose: moving an
    #: agent between departments must not drag its already-pending approvals into
    #: the new department's queue, where a person who was never asked would find
    #: them. `Task.department_id` is deliberately never consulted -- a task can be
    #: handed across a department boundary, and the agent that is about to act is
    #: the one whose authority is in question.
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Display fields backing the approvals inbox / escalations.
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    amount_text: Mapped[str | None] = mapped_column(Text)
    #: Which of the agent's proposed options a human picked (action_type
    #: "decision"). NULL for a plain yes/no on a held tool call, which has none.
    decision_option: Mapped[str | None] = mapped_column(Text)
    #: Structured sibling of `detail`: `{"code": "<Decision.reason_code>",
    #: **Decision.context}` when this approval was raised from a PDP
    #: `authorize_tool_call` REQUIRE_APPROVAL decision, letting the approval
    #: pane render a real i18n'd sentence instead of `detail`'s raw, un-i18n'd
    #: English string. NULL for every approval NOT raised from such a decision
    #: (e.g. `request_decision`, budget incidents, blast-radius stops) --
    #: those keep `detail`/`amount_text` as their only "why".
    reason_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Deep link to the record this approval is about, resolved ONCE at raise
    #: time from the tool pack's own `record_url` template (§6 of the AI
    #: workplace design; `oc8.approvals.record_url`). Stored rather than
    #: computed on read for two reasons: the template lives on a plugin
    #: manifest that may be edited or uninstalled later, and every channel
    #: (inbox, dashboard widget, Telegram, WhatsApp, Teams) is a view over
    #: this row -- one write, five renderers.
    #:
    #: NULL for every approval whose connection declares no template, for
    #: every approval not raised from a tool call at all (budget incidents,
    #: hire requests, `request_decision`), and for every row written before
    #: this column existed. The absence must never block an approval.
    record_url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired')",
            name="ck_approval_status",
        ),
    )


class AuditEvent(Base, PkMixin, TenantMixin):
    """Append-only, per-tenant hash chain (tech-spec §12.5)."""

    __tablename__ = "audit_event"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    decision: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    # Write-time attribution (§12.5 A3): the principal ultimately responsible for
    # this event, resolved via resolve_responsible(). type in
    # operator|agent|plugin|tenant; id is the operator/plugin subject string, the
    # agent UUID as text, or the tenant UUID as text. NULL on pre-A3 rows only.
    responsible_type: Mapped[str | None] = mapped_column(Text)
    responsible_id: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Which compression function produced `hash` (§12.5 hardening).
    # 0 = sha256(prev || canonical) -- every row written before the MAC slice.
    # 1 = HMAC-SHA256(audit_mac_key(), prev || canonical).
    # Monotonic per tenant: a later row may never carry a LOWER version, or an
    # attacker could forge rows as version 0, which needs no secret at all.
    mac_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", default=0
    )


class AuditChainCheckpoint(Base, PkMixin, TenantMixin):
    """How far this tenant's audit chain has been verified (tech-spec §12.5).

    Mutable on purpose -- unlike audit_event this is not in the immutable set,
    because the tamper-detection job advances it on every run.
    """

    __tablename__ = "audit_chain_checkpoint"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_audit_checkpoint_tenant"),)

    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    broken_at_seq: Mapped[int | None] = mapped_column(BigInteger)
    # Monotonic high-water mark: the highest seq this tenant's chain has EVER
    # been observed to reach. Unlike last_seq (which retreats to the last-good
    # position on a break) this only ever increases, so a head-truncation
    # cannot be laundered by the retreat -- see integrity._run.
    max_seen_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # How many rows existed at or below max_seen_seq the last time the chain
    # verified cleanly. The MARK alone is not an invariant: seq is a GLOBAL
    # identity column, so after rows are deleted the tenant's tail climbs back
    # over any mark as soon as one new event is appended. A count of rows at or
    # below a FIXED mark cannot be inflated by later appends (their seq is
    # above the mark), so it stays below this figure until the missing rows
    # actually come back. Advanced only by a clean pass -- see integrity._run.
    verified_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # Why the chain is broken: 'hash_mismatch' (a row's contents or link no
    # longer hash correctly -- a full re-verification is the remedy path after
    # a legitimate restore) or 'truncation' (rows are simply gone; only the
    # rows coming back can clear it). NULL whenever status is not 'broken'.
    break_kind: Mapped[str | None] = mapped_column(Text)
    # Set the first time a break is observed and NEVER cleared. status may
    # legitimately return to "ok" (the operator's acknowledgement after a full
    # re-verification), but the fact that integrity was once in doubt must
    # survive that acknowledgement.
    first_break_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # The highest mac_version this tenant's chain has ever reached, and a MAC
    # over the checkpoint's own state (§12.5). The chain cannot protect this
    # by itself: mac_version is a plain column, so an attacker with UPDATE
    # could set every row to 0 and recompute the whole chain unkeyed --
    # monotonic, self-consistent, and requiring no key. The signed memory of
    # having reached version 1 is what makes that detectable.
    max_mac_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0", default=0
    )
    # How many rows at or below max_seen_seq carried max_mac_version or better
    # at the last CLEAN pass. Bound by state_mac, so it cannot be lowered
    # without the key -- which is the whole point: a wholesale downgrade drops
    # the live figure to 0 and no honest append can raise it again (their seq
    # is above the mark).
    #
    # Stored rather than re-derived live at verification time, because it is
    # stable against appends but NOT against deletions -- and a deletion below
    # the mark is a TRUNCATION, the one break the system is designed to recover
    # from. Deriving it live meant a break-path re-sign (which must happen: the
    # message binds the resumption pair, and _apply retreats that pair) baked
    # the SHORT count into the marker while rows were missing, so restoring the
    # rows -- the documented remedy -- made the marker fail and reported a
    # permanent mac_downgrade. Advanced only by a clean pass; break paths
    # re-sign with the value already stored here.
    keyed_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    state_mac: Mapped[bytes | None] = mapped_column(LargeBinary)
    verified_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TokenUsageRecord(Base, PkMixin, TenantMixin):
    __tablename__ = "token_usage_record"

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    saved_tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    saved_tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    platform_units: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    creator_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    skill_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)


class Budget(Base, PkMixin, TenantMixin, TimestampMixin):
    """Per-tenant or per-department token budget (§15.4).
    department_id NULL means tenant-wide."""

    __tablename__ = "budget"

    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    soft_limit_tokens: Mapped[int | None] = mapped_column(BigInteger)
    hard_limit_tokens: Mapped[int | None] = mapped_column(BigInteger)
    # A one-time hard-limit override for the current budget window, set when an
    # operator approves a budget_incident (§15.4 A2). While now < override_until,
    # check_budget does not report this scope hard_exceeded. Expires naturally at
    # the window boundary (it is set to the next month start). NULL = no override.
    override_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Informational only -- enforcement (metering/budget.py's check_budget)
    # reads only soft_limit_tokens/hard_limit_tokens, never these. Set once,
    # at write time, when the caller supplied a $ amount instead of a token
    # count (see api/v1/budgets.py's upsert_budget). NULL when the budget was
    # set directly in tokens.
    dollar_budget_usd: Mapped[float | None] = mapped_column(Numeric)
    dollar_reference_provider: Mapped[str | None] = mapped_column(Text)
    dollar_reference_model: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("tenant_id", "department_id", name="uq_budget_scope"),
        Index(
            "uq_budget_tenant_wide",
            "tenant_id",
            unique=True,
            postgresql_where=text("department_id IS NULL"),
        ),
    )


class ActivityEvent(Base, PkMixin, TenantMixin):
    __tablename__ = "activity_event"

    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    #: Locale overlays for demo-seeded rows (`{de: {message, detail}}`). Empty
    #: on every live activity -- real runs write English (or the operator's
    #: language) into `message`/`detail` directly and never touch this.
    i18n: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Whether the model turn this activity reports on was answered from the
    # department response cache instead of a real model call (oc8.agent.
    # cache_flow) -- surfaced so an operator watching the feed can tell a
    # genuinely fresh answer apart from a replayed one, instead of only
    # seeing it in TokenUsageRecord.cache_hit, which the feed never reads.
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('success','warning','error','info')", name="ck_activity_status"
        ),
    )


class Integration(Base, PkMixin, TenantMixin, TimestampMixin):
    """Catalog of connectable enterprise systems (Integrations screen)."""

    __tablename__ = "integration"

    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="")
    connected: Mapped[bool] = mapped_column(nullable=False, default=False)
    config_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hue: Mapped[int] = mapped_column(nullable=False, default=0)
    used_by: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
