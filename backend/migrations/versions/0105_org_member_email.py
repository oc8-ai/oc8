"""org_member.email -- the address a person signs in with, as the identity
provider last reported it.

`subject` stays the identity. With an external OIDC provider it is the token's
`sub`, often a uuid, and the members screen labelled that uuid "Sign-in email".
This column carries the token's `email` claim so the screen can show the
address instead. It is display data only: nothing looks a person up by it,
so it has no index and no uniqueness.

Local password sign-in uses the email AS the subject, so existing rows whose
subject looks like an address are backfilled with it.

Revision ID: 0105
Revises: 0104
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0105"
down_revision: str | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def backfill_email(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "UPDATE org_member SET email = subject "
            "WHERE email IS NULL AND subject ~ '^[^@[:space:]]+@[^@[:space:]]+$'"
        )
    )


def upgrade() -> None:
    op.execute("ALTER TABLE org_member ADD COLUMN IF NOT EXISTS email text")
    backfill_email(op.get_bind())


def downgrade() -> None:
    op.execute("ALTER TABLE org_member DROP COLUMN IF EXISTS email")
