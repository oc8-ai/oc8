"""member_guardrail -- a member's own tightening of one agent's tools (D3a),
and approval routing to that member.

Revision ID: 0106
Revises: 0105
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0106"
down_revision: str | None = "0105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS member_guardrail (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL REFERENCES org_member(id) ON DELETE CASCADE,
            agent_id uuid NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
            policy jsonb NOT NULL DEFAULT '{}'::jsonb,
            config_revision integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_member_guardrail UNIQUE (tenant_id, member_id, agent_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_member_guardrail_tenant_id ON member_guardrail (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_member_guardrail_agent_id ON member_guardrail (agent_id)"
    )
    op.execute("ALTER TABLE member_guardrail ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON member_guardrail
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON member_guardrail TO oc8_app;
          END IF;
        END $$
    """)
    # oc8_bump_copilot_config_revision() (migration 0054) bumps NEW.config_revision.
    op.execute("DROP TRIGGER IF EXISTS trg_member_guardrail_config_revision ON member_guardrail")
    op.execute(
        "CREATE TRIGGER trg_member_guardrail_config_revision BEFORE UPDATE ON member_guardrail "
        "FOR EACH ROW EXECUTE FUNCTION oc8_bump_copilot_config_revision()"
    )
    op.execute("ALTER TABLE approval_request ADD COLUMN IF NOT EXISTS assignee_member_id uuid")
    op.execute("ALTER TABLE approval_request ADD COLUMN IF NOT EXISTS previous_approval_id uuid")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_approval_request_assignee "
        "ON approval_request (assignee_member_id) WHERE assignee_member_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_approval_request_assignee")
    op.execute("ALTER TABLE approval_request DROP COLUMN IF EXISTS previous_approval_id")
    op.execute("ALTER TABLE approval_request DROP COLUMN IF EXISTS assignee_member_id")
    op.execute("DROP TABLE IF EXISTS member_guardrail")
