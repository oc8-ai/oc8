"""Durable agent run — the persisted run state machine (Slice 1)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class AgentRun(Base, PkMixin, TenantMixin, TimestampMixin):
    """Durable agent run.

    `idempotency_key` provides exact-once intake across *all* states: a
    webhook redelivered after the run has already completed must still not
    fire a second run, so uniqueness is enforced tenant-wide regardless of
    the row's current `state`. `coalesce_key` is best-effort only — it
    collapses a new intake onto an existing run that is still `queued`
    (i.e. hasn't started yet), and callers are expected to fall back to
    creating a new run when no such candidate is found.
    """

    __tablename__ = "agent_run"

    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # The `AgentVersion` this run's behavioural config is pinned to, so a
    # publish mid-run cannot change what a running agent is doing. Null for
    # every run from before this column existed (0099 does not backfill
    # `agent_run` -- inventing a version for a historical run would be a claim
    # about the past nobody can support) -- a reader treats null as "read the
    # live `agent` row", the same fallback `Agent.current_version_id` being
    # null gets.
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    phase: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    messages: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    coalesce_key: Mapped[str | None] = mapped_column(Text)
    coalesced_count: Mapped[int] = mapped_column(nullable=False, server_default="0")

    #: What became of this run's evidence -- the session folder that answers WHY
    #: (§12.5.1). 'present' on disk as the runtime left it, 'archived' into one
    #: compressed file whose hash is in the audit chain, 'reduced' to that ledger
    #: entry once the evidence window closed, 'none' if the run never wrote any.
    #: This is the evidence sweep's work list; see oc8.evidence.sweep.
    evidence_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="present")
    evidence_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','waiting_for_input',"
            "'waiting_for_approval','failed','done','interrupted')",
            name="ck_agent_run_state",
        ),
        CheckConstraint(
            "source IN "
            "('manual','cron','event','webhook','delegation','decision','handoff','chat')",
            name="ck_agent_run_source",
        ),
        CheckConstraint(
            "evidence_state IN ('present','archived','reduced','none')",
            name="ck_agent_run_evidence_state",
        ),
    )


class Clarification(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "clarification"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")

    __table_args__ = (
        CheckConstraint("status IN ('open','answered')", name="ck_clarification_status"),
    )


class RunCancellation(Base, PkMixin, TenantMixin):
    """Operator cancel signal for a run (§7.2, Paperclip A5).

    Recorded by POST /runs/{id}/cancel on a non-terminal run. Deliberately a
    SEPARATE table, not a column on agent_run: a running executor holds an
    exclusive row lock on the agent_run tuple for the whole run (its early
    transition to 'running' UPDATEs that row inside one long transaction, and
    Postgres row locks are per-tuple), so a cancel write to that row would block
    until the run ends -- the run would hold its own row locked against the very
    write meant to stop it. This row is unlocked, so the endpoint's insert
    commits immediately and the executor observes it under READ COMMITTED.

    The executor is the SOLE mutator of agent_run.state; this row only records
    intent. One row per run (unique run_id) -- its presence is the cancel flag.
    """

    __tablename__ = "run_cancellation"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancellation_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="operator_interrupted"
    )


class RunMessage(Base, PkMixin, TenantMixin, TimestampMixin):
    """A message an operator sends to a RUNNING agent (live steering).

    A SEPARATE, append-only table for the same reason as RunCancellation: the
    executor holds a long transaction with an exclusive lock on the agent_run
    row, so this row is written by the endpoint and observed by the running
    executor under READ COMMITTED at the next step boundary. `delivered` flips
    to true once the engine has injected the message into the agent's context,
    so each message is delivered exactly once."""

    __tablename__ = "run_message"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    author: Mapped[str] = mapped_column(Text, nullable=False, default="operator")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    delivered: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")


class ToolInvocation(Base, PkMixin, TenantMixin, TimestampMixin):
    """One executed side-effectful tool call, so a repeat does not repeat the act.

    Prerequisite for a self-driving runtime (§8.7 R5): without checkpoints a killed
    container restarts its task from the beginning, and for an agent that writes to
    an external system that means doing the write twice. This row is what lets the
    second attempt return the FIRST result instead of acting again.

    Keyed on the TASK, not the run: a resumed leg continues the same task under a
    different run, so the task is the only identity that survives a restart.
    Read-only calls are deliberately not recorded -- deduplicating a search would
    hide changes the agent is supposed to see.
    """

    __tablename__ = "tool_invocation"

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    # Order-insensitive hash of the arguments: a harness may serialise the same
    # call differently between attempts, and that must still count as the same call.
    args_hash: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "tool", "args_hash", name="uq_tool_invocation_call"
        ),
    )


class RecordClaim(Base, PkMixin, TenantMixin, TimestampMixin):
    """Which run is currently working a particular record.

    Two agents polling one queue are handed the same oldest item -- they read it
    in the same second, and a mission rule ("claim the ticket before you write")
    cannot close a race it does not know about. The customer then gets two
    answers, or two different ones.

    On the RECORD rather than on the ticket system: which record a call touches
    comes from the connection's own focus_spec, so this holds for any software a
    plugin connects and the core stays ignorant of all of them.

    The unique constraint is the whole mechanism. The database decides the race,
    not the agents -- anything decided in application code would be a check
    followed by a write, with the other agent's write in between.
    """

    __tablename__ = "record_claim"

    entity: Mapped[str] = mapped_column(Text, nullable=False)
    record_ref: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "entity", "record_ref", name="uq_record_claim"),
    )
