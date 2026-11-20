"""provider_budget -- monthly token limit per model provider (oc8 AI design
2026-10-10 §1.3). Counted from token_usage_record, so no usage column here.

Revision ID: 0104
Revises: 0103
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0104"
down_revision: str | None = "0103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS provider_budget (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            provider text NOT NULL,
            monthly_token_limit bigint,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_budget_tenant_provider UNIQUE (tenant_id, provider)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_provider_budget_tenant_id ON provider_budget (tenant_id)")
    op.execute("ALTER TABLE provider_budget ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON provider_budget
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    # Said out loud like 0046 does: a database restored from a dump has no
    # default privileges, and the runtime role would get `permission denied`.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON provider_budget TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_budget")
