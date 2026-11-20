"""Passkeys as a second factor.

Adds `passkey_challenge` (the random challenge a passkey signs, short-lived
and single use, bound to a member and a purpose), tenant-isolated by RLS like
`email_otp_challenge`. The passkeys themselves are `second_factor` rows
(0127); a unique index keeps one credential from being registered twice in
an organisation.

Revision ID: 0128
Revises: 0127
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0128"
down_revision: str | None = "0127"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS passkey_challenge (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL,
            purpose text NOT NULL,
            challenge text NOT NULL,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_passkey_challenge_purpose CHECK (purpose IN ('register','login'))
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_passkey_challenge_tenant_id ON passkey_challenge (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_passkey_challenge_member_id ON passkey_challenge (member_id)"
    )
    op.execute("ALTER TABLE passkey_challenge ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON passkey_challenge
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON passkey_challenge TO oc8_app;
          END IF;
        END $$
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_second_factor_passkey_credential "
        "ON second_factor (tenant_id, (data->>'credential_id')) WHERE method = 'passkey'"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS uq_second_factor_passkey_credential")
    op.execute("DROP TABLE IF EXISTS passkey_challenge")
