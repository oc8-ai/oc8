"""Data boards: a stored view plus its own append-only record store.

`data_board_batch` and `data_board_record` are append-only by grant: the app
role gets no UPDATE (history is "as reported then", never rewritten).

Revision ID: 0122
Revises: 0121
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0122"
down_revision: str | None = "0121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("data_board", "data_board_spec_version", "data_board_batch", "data_board_record")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS data_board (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            department_id uuid NOT NULL REFERENCES department(id) ON DELETE CASCADE,
            agent_id uuid REFERENCES agent(id) ON DELETE SET NULL,
            title text NOT NULL,
            description text NOT NULL DEFAULT '',
            spec jsonb NOT NULL,
            spec_version integer NOT NULL DEFAULT 1,
            removed_at timestamptz,
            deleted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_data_board_title CHECK (char_length(title) BETWEEN 1 AND 200)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS data_board_spec_version (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            board_id uuid NOT NULL REFERENCES data_board(id) ON DELETE CASCADE,
            version integer NOT NULL,
            title text NOT NULL,
            spec jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_data_board_spec_version UNIQUE (board_id, version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS data_board_batch (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            board_id uuid NOT NULL REFERENCES data_board(id) ON DELETE CASCADE,
            dataset text NOT NULL,
            as_of timestamptz NOT NULL,
            run_id uuid,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            row_count integer NOT NULL,
            note text,
            CONSTRAINT ck_data_board_batch_row_count CHECK (row_count >= 1),
            CONSTRAINT ck_data_board_batch_note CHECK (char_length(note) <= 2000)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS data_board_record (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            board_id uuid NOT NULL REFERENCES data_board(id) ON DELETE CASCADE,
            batch_id uuid NOT NULL REFERENCES data_board_batch(id) ON DELETE CASCADE,
            dataset text NOT NULL,
            as_of timestamptz NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            run_id uuid,
            values jsonb NOT NULL
        )
    """)
    for table in _TABLES:
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_data_board_department "
        "ON data_board (tenant_id, department_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_data_board_agent ON data_board (agent_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_data_board_batch_period "
        "ON data_board_batch (board_id, dataset, as_of, recorded_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_data_board_record_period "
        "ON data_board_record (board_id, dataset, as_of)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_data_board_record_batch ON data_board_record (batch_id)"
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            DO $$ BEGIN
                CREATE POLICY tenant_isolation ON {table}
                    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
        """)
    # Explicit grants (default privileges may or may not exist), and an explicit
    # REVOKE of UPDATE where default privileges would have granted it: the
    # history tables are append-only by grant.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON data_board TO oc8_app;
            GRANT SELECT, INSERT, DELETE ON data_board_spec_version TO oc8_app;
            GRANT SELECT, INSERT, DELETE ON data_board_batch TO oc8_app;
            GRANT SELECT, INSERT, DELETE ON data_board_record TO oc8_app;
            REVOKE UPDATE ON data_board_spec_version FROM oc8_app;
            REVOKE UPDATE ON data_board_batch FROM oc8_app;
            REVOKE UPDATE ON data_board_record FROM oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
