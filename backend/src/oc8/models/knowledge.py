"""Knowledge platform, memory, and MCP connections (tech-spec §6.2, §10, §11)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, SoftDeleteMixin, TenantMixin

#: Must match what `Settings.default_embedding_model` actually produces. It said
#: 1536 (OpenAI's size) while the default model has long been nomic-embed-text,
#: which produces 768 -- so every ingest died with `expected 1536 dimensions, not
#: 768` and the knowledge base was unusable out of the box. Changing the
#: embedding model means changing this AND re-embedding everything stored.
EMBED_DIM = 768


class DataSource(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """A deleted source is retired, not vaporised: its IngestionJob rows and the
    tombstones of everything it ever ingested still point at it."""

    __tablename__ = "data_source"

    connector_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    oauth_connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    classification: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    schedule_cron: Mapped[str | None] = mapped_column(Text)
    connected: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: Text holding an isoformat string, for historical reasons. Use
    #: `last_attested_sync_at` as the model for any new timestamp here.
    last_sync_at: Mapped[str | None] = mapped_column(Text)
    doc_count: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Stamped ONLY by a sync that carried an attestation and hit no
    #: connector-level fatal. A real timestamptz, not Text like `last_sync_at`,
    #: because its whole job is the comparison against `KbChunk.missing_since`.
    last_attested_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 'held' = core refused an attested listing as implausible and is doing
    #: nothing to this source until a human looks. A state column, like
    #: `agent_run.evidence_state`, so the situation is not re-derived every tick.
    reconcile_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="ok", server_default="ok"
    )
    #: Why it is held, in words. A hold with no reason sends an operator hunting
    #: a bug in the sweep.
    reconcile_note: Mapped[str | None] = mapped_column(Text)
    #: Set at the end of the last completed sync (`run_source_sync`'s single
    #: funnel). null = never synced yet. Deliberately separate from
    #: `connected` (transport/credentials reachable), which a failed sync does
    #: NOT move -- without this a red sync failure was invisible behind an
    #: unrelated green "connected" dot.
    last_sync_status: Mapped[str | None] = mapped_column(Text)
    #: Human-readable reason for the last "failed" sync. null once
    #: `last_sync_status` is "ok" again, or before the first sync ever ran.
    last_sync_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "classification IN ('public','internal','confidential','restricted')",
            name="ck_source_classification",
        ),
        CheckConstraint(
            "reconcile_state = ANY (ARRAY['ok','held'])",
            name="ck_data_source_reconcile_state",
        ),
        CheckConstraint(
            "last_sync_status IS NULL OR last_sync_status = ANY (ARRAY['ok','failed'])",
            name="ck_data_source_last_sync_status",
        ),
    )


class IngestionJob(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "ingestion_job"

    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    kb_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','partial')",
            name="ck_job_status",
        ),
    )


class KnowledgeBase(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "knowledge_base"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    chunking_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    classification: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    freshness: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="current")
    local_only: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: ``internal`` (default) searches ``kb_chunk``. Any other value is a
    #: capa ``type_id`` resolved through ``resolve_vector_index`` — query-only,
    #: no ingest into oc8.
    index_type: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    #: Non-secret mapping for an external index (collection, table, field keys).
    #: Secrets live on ``credential_id``, never here.
    index_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Required when ``index_type`` is not ``internal``. Points at a
    #: ``Credential`` whose type matches the capa's ``credential_type``.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)


class KbChunk(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """A tombstoned chunk is a record that a document was removed, not a document.

    Document identity is (tenant_id, data_source_id, kb_id, source_uri). `kb_id`
    belongs in it because a DataSource has no KB of its own -- the target base is
    a per-request parameter of the sync route -- so one source legitimately feeds
    several bases, and an identity that omitted it would delete out of all of
    them at once.
    """

    __tablename__ = "kb_chunk"

    kb_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    classification: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    raw_object_key: Mapped[str | None] = mapped_column(Text)
    #: NULL means "written before 0045, provenance unknown", and such a row is
    #: never eligible for reconciliation -- unknown is not the same as absent.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: 'operator_delete' | 'source_absent' | 'superseded'. Not cosmetic: it
    #: decides reduction timing, write-path suppression and cursor eviction.
    deleted_reason: Mapped[str | None] = mapped_column(Text)
    #: When the content was actually destroyed. Split from `deleted_at` so that a
    #: deletion the system inferred stays recoverable for a grace window.
    reduced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: When an attested listing FIRST omitted this document; cleared by any later
    #: authoritative listing containing it. A timestamp rather than a boolean so
    #: "a second attested sync a window later agreed" is one SQL comparison.
    missing_since: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(deleted_at IS NULL AND deleted_reason IS NULL) OR "
            "(deleted_at IS NOT NULL AND deleted_reason = ANY "
            "(ARRAY['operator_delete','source_absent','superseded']))",
            name="ck_kb_chunk_deleted_reason",
        ),
        # A reduced row that still holds retrievable text is unrepresentable, so
        # "prove the document is gone" is a count(*) over content rather than an
        # audit of every path that might have forgotten to blank it.
        CheckConstraint(
            "reduced_at IS NULL OR (deleted_at IS NOT NULL AND content = '' AND embedding IS NULL)",
            name="ck_kb_chunk_reduced_is_empty",
        ),
    )


class KnowledgeGrant(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_grant"

    kb_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    grantee_type: Mapped[str] = mapped_column(Text, nullable=False)
    grantee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint("grantee_type IN ('department','agent')", name="ck_grant_grantee"),
        UniqueConstraint("tenant_id", "kb_id", "grantee_type", "grantee_id", name="uq_grant"),
    )


class McpConnection(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "mcp_connection"

    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    server_url: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False, default="stdio")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Either a flat list of tool names, or -- what every plugin actually ships --
    # a right -> tools mapping ({"read": [...], "send": [...]}) that the frame
    # check needs to tell a lookup from a write. Both shapes are stored, and the
    # gateway branches on isinstance, so the declaration says both.
    scopes: Mapped[list[Any] | dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    # A Credential-backed login (agent tool login selection design). NULL for
    # every existing, department-scoped OAuth connection -- that flow (see
    # configure_plugin) is untouched and keeps using department_id instead.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    connected: Mapped[bool] = mapped_column(nullable=False, default=False)


class MemoryStore(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "memory_store"

    tier: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        CheckConstraint("tier IN ('agent','department','company')", name="ck_memory_tier"),
        UniqueConstraint("tenant_id", "tier", "owner_id", name="uq_memory_store_tenant_tier_owner"),
    )


class MemoryRecord(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "memory_record"

    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    record_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    written_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="approved")

    __table_args__ = (
        CheckConstraint(
            "status IN ('approved','pending','rejected')", name="ck_memory_record_status"
        ),
    )
