"""oc8 Chat C3: pinned context per session, projects, pinned chats.

Revision ID: 0113
Revises: 0112
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0113"
down_revision: str | None = "0112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Fail fast rather than queue the app behind a run's transaction (0112).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS pinned_refs jsonb "
        "NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS project_id uuid")
    op.execute(
        "ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_session_project_id ON chat_session (project_id)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_project (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            owner_member_id uuid NOT NULL,
            name text NOT NULL,
            instructions text NOT NULL DEFAULT '',
            pinned_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            shared_with jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_chat_project_name CHECK (char_length(name) BETWEEN 1 AND 120),
            CONSTRAINT ck_chat_project_instructions CHECK (char_length(instructions) <= 8000)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_project_tenant_id ON chat_project (tenant_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_project_tenant_owner "
        "ON chat_project (tenant_id, owner_member_id)"
    )
    op.execute("ALTER TABLE chat_project ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON chat_project
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    # Said out loud like 0112 does: a database restored from a dump has no
    # default privileges, and the runtime role would get `permission denied`.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON chat_project TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    # Fail fast rather than queue the app behind a run's transaction (0112).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS chat_project")
    op.execute("DROP INDEX IF EXISTS ix_chat_session_project_id")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS pinned")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS project_id")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS pinned_refs")
