"""Two-factor policy and email codes.

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


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE organization DROP COLUMN IF EXISTS totp_step_up_enabled")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE organization ADD COLUMN IF NOT EXISTS totp_step_up_enabled "
        "boolean NOT NULL DEFAULT false"
    )
