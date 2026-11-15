"""Copilot direct access: a conversation's grant to act through one connection.

One active row per (conversation, tool key); revoking keeps the row for the
audit trail. Deleting the conversation deletes its grants (FK cascade), and a
grant cannot outlive the approval that created it.

Revision ID: 0120
Revises: 0119
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS direct_access_grant (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            chat_session_id uuid NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
            member_id uuid NOT NULL,
            tool_key text NOT NULL,
            approval_id uuid NOT NULL REFERENCES approval_request(id) ON DELETE CASCADE,
            granted_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            actions integer NOT NULL DEFAULT 0,
            agent_suggested_at timestamptz,
            CONSTRAINT ck_direct_access_grant_tool_key
                CHECK (char_length(tool_key) BETWEEN 1 AND 200)
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_direct_access_grant_active "
        "ON direct_access_grant (tenant_id, chat_session_id, tool_key) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_direct_access_grant_tenant_member "
        "ON direct_access_grant (tenant_id, member_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_direct_access_grant_tenant_id "
        "ON direct_access_grant (tenant_id)"
    )
    op.execute("ALTER TABLE direct_access_grant ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON direct_access_grant
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON direct_access_grant TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS direct_access_grant")
