"""org_member.onboarding_status -- a person's own first-run welcome.

`organization.onboarding_status` is the organisation's setup; this is the same
idea for one member, read by an edition that greets members (core itself shows
nothing for it). Everybody already in a workspace becomes 'completed', so
nobody is sent through a welcome they never needed; rows created afterwards
start 'pending'.

Revision ID: 0106
Revises: 0105
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0106"
down_revision: str | None = "0105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def add_onboarding_status(conn: sa.Connection) -> None:
    # The column default fills every existing row with 'completed'; the default
    # then changes for rows created from now on.
    conn.execute(
        sa.text(
            "ALTER TABLE org_member ADD COLUMN IF NOT EXISTS onboarding_status text "
            "NOT NULL DEFAULT 'completed'"
        )
    )
    conn.execute(
        sa.text("ALTER TABLE org_member ALTER COLUMN onboarding_status SET DEFAULT 'pending'")
    )
    conn.execute(
        sa.text(
            "ALTER TABLE org_member ADD CONSTRAINT ck_org_member_onboarding_status "
            "CHECK (onboarding_status IN ('pending','completed','skipped'))"
        )
    )


def upgrade() -> None:
    add_onboarding_status(op.get_bind())


def downgrade() -> None:
    op.execute("ALTER TABLE org_member DROP CONSTRAINT IF EXISTS ck_org_member_onboarding_status")
    op.execute("ALTER TABLE org_member DROP COLUMN IF EXISTS onboarding_status")
