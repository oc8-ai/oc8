"""Triggers: what the creator saw when they last saved the trigger.

`trigger.access_fingerprint` is the agent's access fingerprint (agent version,
skills, department frame and tool setups) taken when the trigger's creator
creates or saves it. A run the trigger starts acts through the creator's own
account only while the fingerprint is unchanged; NULL (every trigger saved
before this revision) means the creator must save it once again.

Revision ID: 0110
Revises: 0109
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0110"
down_revision: str | None = "0109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def add_access_fingerprint(conn: sa.Connection) -> None:
    conn.execute(sa.text("ALTER TABLE trigger ADD COLUMN IF NOT EXISTS access_fingerprint text"))


def remove_access_fingerprint(conn: sa.Connection) -> None:
    conn.execute(sa.text("ALTER TABLE trigger DROP COLUMN IF EXISTS access_fingerprint"))


def upgrade() -> None:
    add_access_fingerprint(op.get_bind())


def downgrade() -> None:
    remove_access_fingerprint(op.get_bind())
