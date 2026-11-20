"""Polled event sources.

A trigger on a polled event names the login it listens on
(`trigger.event_target_credential_id`) and carries the poller's settings
(`trigger.event_config`). `event_source_cursor` keeps, per trigger, where the
poller left off and how its last polls went; tenant-isolated by RLS like
`passkey_challenge`, and never backed up (a restored trigger starts from a
fresh baseline).

Revision ID: 0129
Revises: 0128
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0129"
down_revision: str | None = "0128"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE trigger ADD COLUMN IF NOT EXISTS event_target_credential_id uuid NULL "
        "REFERENCES credential(id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE trigger ADD COLUMN IF NOT EXISTS event_config jsonb NOT NULL "
        "DEFAULT '{}'::jsonb"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trigger_event_target_credential_id "
        "ON trigger (event_target_credential_id)"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS event_source_cursor (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            trigger_id uuid NOT NULL REFERENCES trigger(id) ON DELETE CASCADE,
            cursor jsonb NULL,
            last_polled_at timestamptz,
            last_ok_at timestamptz,
            last_error text,
            last_error_at timestamptz,
            failures integer NOT NULL DEFAULT 0,
            next_poll_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_event_source_cursor_trigger UNIQUE (trigger_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_event_source_cursor_tenant_id "
        "ON event_source_cursor (tenant_id)"
    )
    op.execute("ALTER TABLE event_source_cursor ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON event_source_cursor
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON event_source_cursor TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS event_source_cursor")
    op.execute("DROP INDEX IF EXISTS ix_trigger_event_target_credential_id")
    op.execute("ALTER TABLE trigger DROP COLUMN IF EXISTS event_config")
    op.execute("ALTER TABLE trigger DROP COLUMN IF EXISTS event_target_credential_id")
