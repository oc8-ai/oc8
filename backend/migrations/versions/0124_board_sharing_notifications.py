"""Board shares and per-member notifications.

`data_board_share` grants read access to one board to a member or a department.
`member_notification` is a per-member inbox; its partial unique index collapses
repeated unread notifications for one `(member_id, kind, subject_id)`, and is
NULLS NOT DISTINCT so a NULL subject collapses too (needs PostgreSQL 15+, the
pinned image). `member_notification_setting` holds per-kind channel opt-outs.

Revision ID: 0124
Revises: 0123
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0124"
down_revision: str | None = "0123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("data_board_share", "member_notification", "member_notification_setting")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS data_board_share (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            board_id uuid NOT NULL REFERENCES data_board(id) ON DELETE CASCADE,
            member_id uuid REFERENCES org_member(id) ON DELETE CASCADE,
            department_id uuid REFERENCES department(id) ON DELETE CASCADE,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_data_board_share_one_target
                CHECK ((member_id IS NULL) <> (department_id IS NULL)),
            CONSTRAINT uq_data_board_share_member UNIQUE (board_id, member_id),
            CONSTRAINT uq_data_board_share_department UNIQUE (board_id, department_id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS member_notification (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL REFERENCES org_member(id) ON DELETE CASCADE,
            kind text NOT NULL,
            subject_id uuid,
            title jsonb NOT NULL,
            body jsonb NOT NULL,
            link text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            read_at timestamptz
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS member_notification_setting (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL REFERENCES org_member(id) ON DELETE CASCADE,
            kind text NOT NULL,
            push_enabled boolean NOT NULL DEFAULT true,
            messenger_enabled boolean NOT NULL DEFAULT true,
            CONSTRAINT uq_member_notification_setting UNIQUE (member_id, kind)
        )
    """)
    for table in _TABLES:
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_data_board_share_member ON data_board_share (member_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_data_board_share_department "
        "ON data_board_share (department_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_member_notification_member_created "
        "ON member_notification (member_id, created_at DESC)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_member_notification_unread "
        "ON member_notification (member_id, kind, subject_id) NULLS NOT DISTINCT "
        "WHERE read_at IS NULL"
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
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON data_board_share TO oc8_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON member_notification TO oc8_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON member_notification_setting TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
