"""Usage limits on guardrails.

A guardrail may now say "at most 50 of these calls a day"
(`authz.pdp.UsageLimit`). `tool_usage_counter` holds the count: one row per
agent, tool key, limit and window, incremented by a single conditional upsert
so two parallel runs cannot both take the last call. Tenant-isolated by RLS
like `event_source_cursor`, never backed up (runtime state; a restored agent
starts its day from zero) and pruned once a window is old. No foreign keys:
the count is taken in its own short transaction, see the model.

Revision ID: 0130
Revises: 0129
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0130"
down_revision: str | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS tool_usage_counter (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            agent_id uuid NOT NULL,
            tool_key text NOT NULL,
            tools_key text NOT NULL,
            period text NOT NULL,
            window_start timestamptz NOT NULL,
            count integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_tool_usage_counter_window
                UNIQUE (tenant_id, agent_id, tool_key, tools_key, period, window_start),
            CONSTRAINT ck_tool_usage_counter_count CHECK (count >= 0)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_usage_counter_tenant_id "
        "ON tool_usage_counter (tenant_id)"
    )
    # The prune walks old windows.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_usage_counter_window_start "
        "ON tool_usage_counter (window_start)"
    )
    op.execute("ALTER TABLE tool_usage_counter ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON tool_usage_counter
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON tool_usage_counter TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS tool_usage_counter")
