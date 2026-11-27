"""A person lets one write tool run without asking them again ("always allow").

`tool_allow_rule` is one member's standing approval of one tool (`tool_key`,
the tool name the card held) for one agent on one connection (`connection_id`,
NULL for a core tool). Keyed on the connection's id, never its name: a
connection deleted and re-created under the same name is a different system
and matches no old rule. Revoking sets `revoked_at`; the row stays for the
audit. At most one live rule per member, agent, connection and tool.

Revision ID: 0141
Revises: 0140
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0141"
down_revision: str | None = "0140"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS tool_allow_rule (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL,
            agent_id uuid NOT NULL,
            connection_id uuid NULL,
            tool_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz NULL,
            created_from_approval_id uuid NULL
        )
    """)
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE tool_allow_rule ADD CONSTRAINT ck_tool_allow_rule_tool_key
                CHECK (char_length(tool_key) BETWEEN 1 AND 200);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_allow_rule_tenant_id ON tool_allow_rule (tenant_id)"
    )
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_allow_rule_live
        ON tool_allow_rule (
            tenant_id,
            member_id,
            agent_id,
            coalesce(connection_id, '00000000-0000-0000-0000-000000000000'::uuid),
            tool_key
        )
        WHERE revoked_at IS NULL
    """)
    op.execute("ALTER TABLE tool_allow_rule ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON tool_allow_rule
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    # A database restored from a dump has no default privileges (0113).
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON tool_allow_rule TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS tool_allow_rule")
