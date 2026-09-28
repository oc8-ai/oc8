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
about the past we cannot support. The backfill leaves `skill_assignments`/
`knowledge_grants` empty inside the payload: those bindings are already
version-pinned in their own tables, and a mechanical backfill must not claim
a snapshot it did not actually take; `oc8.agents.versioning.snapshot_agent`
populates both for every publish from here on.

`payload_hash` is computed with the built-in `sha256(bytea)` (core Postgres
since v11), not pgcrypto's `digest()` -- pgcrypto is not enabled by any
migration in this project and there is no reason to add it just for a
migration-time placeholder hash that real publishes will immediately
supersede.

Revision ID: 0098
Revises: 0097
"""

from __future__ import annotations

from collections.abc import Sequence

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

    op.execute("""
        INSERT INTO agent_version (id, tenant_id, agent_id, version_no, payload,
                                   payload_hash, published_at, created_at, updated_at)
        SELECT gen_random_uuid(), a.tenant_id, a.id, 1,
               jsonb_build_object(
                 'mission', a.mission, 'role_title', a.role_title,
                 'definition', a.definition, 'model_config_id', a.model_config_id,
                 'narrowing', a.narrowing,
                 'narrowing_overridden_keys', a.narrowing_overridden_keys,
                 'role_id', a.role_id, 'runtime_ref', a.runtime_ref,
                 'is_team_lead', a.is_team_lead,
                 'skill_assignments', '[]'::jsonb, 'knowledge_grants', '[]'::jsonb
               ),
               sha256('backfill-v1'::bytea), now(), now(), now()
        FROM agent a
        WHERE a.deleted_at IS NULL AND a.current_version_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM agent_version v WHERE v.agent_id = a.id AND v.version_no = 1
          )
    """)
    op.execute("""
        UPDATE agent a SET current_version_id = v.id
        FROM agent_version v
        WHERE v.agent_id = a.id AND v.version_no = 1 AND a.current_version_id IS NULL
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE agent_run DROP COLUMN IF EXISTS agent_version_id")
    op.execute("ALTER TABLE agent DROP COLUMN IF EXISTS current_version_id")
    op.execute("DROP TABLE IF EXISTS agent_version")
