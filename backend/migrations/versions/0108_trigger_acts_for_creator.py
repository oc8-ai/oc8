"""Triggers: whether a webhook or event trigger acts through its creator.

`trigger.acts_for_creator` is false for every trigger. A schedule always acts
through the person who created it; a webhook or event trigger is fired by
whoever can reach it, so it only borrows its creator's account when that
creator explicitly said so. Editing a trigger as someone else resets it.

Revision ID: 0108
Revises: 0107
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0108"
down_revision: str | None = "0107"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def add_acts_for_creator(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "ALTER TABLE trigger ADD COLUMN IF NOT EXISTS acts_for_creator boolean "
            "NOT NULL DEFAULT false"
        )
    )


def remove_acts_for_creator(conn: sa.Connection) -> None:
    conn.execute(sa.text("ALTER TABLE trigger DROP COLUMN IF EXISTS acts_for_creator"))


def upgrade() -> None:
    add_acts_for_creator(op.get_bind())


def downgrade() -> None:
    remove_acts_for_creator(op.get_bind())
