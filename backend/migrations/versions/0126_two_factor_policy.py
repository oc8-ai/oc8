"""Two-factor policy and email codes.

Adds `second_factor` (methods other than TOTP: email now, passkeys later) and
`email_otp_challenge` (short-lived, hashed codes mailed to a member), both
tenant-isolated by RLS like `totp_credential`.

Drops `organization.totp_step_up_enabled`: nothing ever read it, and the
organisation's two-factor policy now lives in `organization.settings
["two_factor"]`.

Revision ID: 0126
Revises: 0125
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0126"
down_revision: str | None = "0125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ("second_factor", "email_otp_challenge")


def _isolate(table: str) -> None:
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_member_id ON {table} (member_id)")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO oc8_app;
          END IF;
        END $$
    """)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS second_factor (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL,
            method text NOT NULL,
            label text NOT NULL DEFAULT '',
            data jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_second_factor_method CHECK (method IN ('email','passkey'))
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_second_factor_member_email "
        "ON second_factor (member_id) WHERE method = 'email'"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS email_otp_challenge (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL,
            purpose text NOT NULL,
            code_hash text NOT NULL,
            expires_at timestamptz NOT NULL,
            attempts integer NOT NULL DEFAULT 0,
            used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_email_otp_challenge_purpose CHECK (purpose IN ('enrol','login'))
        )
    """)
    for table in _TABLES:
        _isolate(table)
    op.execute("ALTER TABLE organization DROP COLUMN IF EXISTS totp_step_up_enabled")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE organization ADD COLUMN IF NOT EXISTS totp_step_up_enabled "
        "boolean NOT NULL DEFAULT false"
    )
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
