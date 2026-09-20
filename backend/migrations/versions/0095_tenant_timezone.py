"""organization.timezone -- IANA zone name for run-context "Now" lines and
step-stamp timestamps (office agent harness Package 3, A2/C3). Defaults to
UTC; oc8.agent.harness.prompts.resolve_timezone falls back safely on any
value zoneinfo does not recognize, so this column is never a hard failure
point.

NOTE: this chains on the locally-untracked 0094_api_key_expires_at.py
(Package 2 ledger). When a real 0094 migration merges in from dev, delete
the local one, renumber this file to whatever comes after the real one, and
fix down_revision accordingly -- this is a known, already-accepted landmine,
not a new one.

Revision ID: 0095
Revises: 0094
Create Date: 2026-09-20

NOTE: 0001_initial.py builds its frozen table set via a live
`Base.metadata.create_all()`, so a column added to an existing 0001-era
model (organization is one) is already present once this code lands --
a plain `op.add_column` collides with it on a from-scratch replay. Uses
`ADD COLUMN IF NOT EXISTS`, same as 0071_totp_credential.py's
`organization.totp_step_up_enabled`, the established precedent for this.
"""

from __future__ import annotations

from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE organization ADD COLUMN IF NOT EXISTS timezone "
        "text NOT NULL DEFAULT 'UTC'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE organization DROP COLUMN IF EXISTS timezone")
