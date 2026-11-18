"""Enable an organisation login for departments.

`credential_department` links one `credential` to the departments it is enabled
for. `credential.all_departments` enables it for every department.
`credential.consolidated_from` records the former `mcp_connection` ids folded
into a login, so that fold can run once.

Revision ID: 0125
Revises: 0124
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0125"
down_revision: str | None = "0124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE credential "
        "ADD COLUMN IF NOT EXISTS all_departments boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE credential ADD COLUMN IF NOT EXISTS consolidated_from jsonb")
    op.execute("""
        CREATE TABLE IF NOT EXISTS credential_department (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            credential_id uuid NOT NULL REFERENCES credential(id) ON DELETE CASCADE,
            department_id uuid NOT NULL REFERENCES department(id) ON DELETE CASCADE,
            CONSTRAINT uq_credential_department UNIQUE (credential_id, department_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_credential_department_tenant_id "
        "ON credential_department (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_credential_department_department_id "
        "ON credential_department (department_id)"
    )
    op.execute("ALTER TABLE credential_department ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON credential_department
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON credential_department TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS credential_department")
    op.execute("ALTER TABLE credential DROP COLUMN IF EXISTS consolidated_from")
    op.execute("ALTER TABLE credential DROP COLUMN IF EXISTS all_departments")
