"""agent_version -- an immutable snapshot of an agent's behavioural config.

The `agent` row is the working copy an operator edits; an `agent_version` is
a snapshot of the behavioural subset that actually runs (mission, role,
definition, model, narrowing, skills, knowledge -- see
`oc8.agents.versioning.snapshot_agent`). `agent.current_version_id` points at
the version that is live; `agent_run.agent_version_id` pins a run to the
version it started with, so a publish mid-run never changes what a running
agent is doing.

`agent_version` is a brand new table, not part of the frozen snapshot 0001
creates on a fresh database, so its own `create table` runs unconditionally
here (guarded with `IF NOT EXISTS` anyway, since this migration must also be
safe to re-run against a database that already has it). `agent`/`agent_run`
are both pre-existing tables -- `agent` happens to be one of the tables 0001
`CREATE TABLE`s from LIVE model metadata, so on a fresh database
`current_version_id` already exists by the time this runs; `agent_run` has
its own dedicated creation migration (0002) untouched by later model edits,
so `agent_version_id` does NOT already exist there. Both columns are added
with `ADD COLUMN IF NOT EXISTS` so upgrade works identically on both a fresh
install and an incremental deploy.

Backfill: every non-deleted agent gets v1 from its current row state, so
there is never an agent without a current version. `agent_run` is NOT
backfilled -- inventing a version for a historical run would be a claim
about the past we cannot support. The backfilled payload is the REAL
payload, not a placeholder: `_agent_payload` below duplicates
`oc8.agents.versioning.snapshot_agent`'s exact column set and
skill-assignment/knowledge-grant filters in raw SQL (a migration must not
import app code, so this is a deliberate duplication -- keep the two in
sync if `snapshot_agent`'s shape ever changes; `tests/migrations/
test_migration_0098_backfill.py` pins the shape from this side). A
placeholder hash would make every migrated agent look permanently "dirty"
to a later draft-status diff, and a rollback to a fake v1 would silently
disable real skill assignments and delete real knowledge grants -- neither
is acceptable for a row nothing has actually changed on yet.

`payload_hash` is computed in Python with the same canonical
`json.dumps(..., sort_keys=True, separators=(",", ":"), default=str)` +
`hashlib.sha256` that `oc8.agents.versioning.payload_hash` uses, then bound
as a parameter -- not a SQL-side digest of anything, and not pgcrypto's
`digest()` (pgcrypto is not enabled by any migration in this project).

Revision ID: 0098
Revises: 0097
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_version (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            agent_id uuid NOT NULL,
            version_no integer NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            payload_hash bytea NOT NULL,
            note text,
            published_by uuid,
            published_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_version_no UNIQUE (agent_id, version_no)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_version_tenant_id ON agent_version (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_version_agent_id ON agent_version (agent_id)")
    op.execute("ALTER TABLE agent_version ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON agent_version
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    op.execute("ALTER TABLE agent ADD COLUMN IF NOT EXISTS current_version_id uuid")
    op.execute("ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS agent_version_id uuid")

    backfill_v1(op.get_bind())


def backfill_v1(conn: sa.engine.Connection) -> None:
    """Give every non-deleted, unversioned agent a real v1, computed the same
    way `oc8.agents.versioning.snapshot_agent` would compute it for that
    agent right now. Factored out of `upgrade()` so a test can call it
    directly against a live connection without running the whole migration
    chain."""
    agents = conn.execute(
        sa.text("""
            SELECT id, tenant_id, mission, role_title, definition, model_config_id,
                   narrowing, narrowing_overridden_keys, role_id, runtime_ref, is_team_lead
            FROM agent
            WHERE deleted_at IS NULL AND current_version_id IS NULL
        """)
    ).all()

    for agent in agents:
        already = conn.execute(
            sa.text("SELECT 1 FROM agent_version WHERE agent_id = :agent_id AND version_no = 1"),
            {"agent_id": agent.id},
        ).first()
        if already is not None:
            continue

        payload = _agent_payload(conn, agent)
        version_id = uuid.uuid4()
        conn.execute(
            sa.text("""
                INSERT INTO agent_version
                    (id, tenant_id, agent_id, version_no, payload, payload_hash,
                     published_at, created_at, updated_at)
                VALUES
                    (:id, :tenant_id, :agent_id, 1, CAST(:payload AS jsonb), :payload_hash,
                     now(), now(), now())
            """),
            {
                "id": version_id,
                "tenant_id": agent.tenant_id,
                "agent_id": agent.id,
                "payload": json.dumps(payload, default=str),
                "payload_hash": _payload_hash(payload),
            },
        )
        conn.execute(
            sa.text("UPDATE agent SET current_version_id = :version_id WHERE id = :agent_id"),
            {"version_id": version_id, "agent_id": agent.id},
        )


def _agent_payload(conn: sa.engine.Connection, agent: sa.engine.Row[Any]) -> dict[str, Any]:
    """Mirrors `oc8.agents.versioning.snapshot_agent`'s exact column set and
    the same `skill_assignment` / `knowledge_grant` filters (agent-scoped,
    not soft-deleted, enabled-only for skills; `grantee_type='agent'` for
    knowledge) -- duplicated here in raw SQL rather than imported, since a
    migration must not depend on app code."""
    payload: dict[str, Any] = {
        "mission": agent.mission,
        "role_title": agent.role_title,
        "definition": agent.definition,
        "model_config_id": str(agent.model_config_id)
        if agent.model_config_id is not None
        else None,
        "narrowing": agent.narrowing,
        "narrowing_overridden_keys": agent.narrowing_overridden_keys,
        "role_id": str(agent.role_id) if agent.role_id is not None else None,
        "runtime_ref": agent.runtime_ref,
        "is_team_lead": agent.is_team_lead,
    }

    assignments = conn.execute(
        sa.text("""
            SELECT skill_version_id FROM skill_assignment
            WHERE agent_id = :agent_id AND deleted_at IS NULL AND enabled IS TRUE
        """),
        {"agent_id": agent.id},
    ).all()
    payload["skill_assignments"] = sorted(
        ({"skill_version_id": str(row.skill_version_id)} for row in assignments),
        key=lambda d: d["skill_version_id"],
    )

    grants = conn.execute(
        sa.text("""
            SELECT kb_id FROM knowledge_grant
            WHERE grantee_type = 'agent' AND grantee_id = :agent_id
        """),
        {"agent_id": agent.id},
    ).all()
    payload["knowledge_grants"] = sorted(str(row.kb_id) for row in grants)

    return payload


def _payload_hash(payload: Mapping[str, Any]) -> bytes:
    """The same canonical form `oc8.agents.versioning.payload_hash` uses --
    duplicated rather than imported for the same reason as `_agent_payload`."""
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).digest()


def downgrade() -> None:
    op.execute("ALTER TABLE agent_run DROP COLUMN IF EXISTS agent_version_id")
    op.execute("ALTER TABLE agent DROP COLUMN IF EXISTS current_version_id")
    op.execute("DROP TABLE IF EXISTS agent_version")
