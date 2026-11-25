"""An assistant message carries a digest of the run's work and a restricted flag.

`chat_message.work_digest` is one line per tool call the run made (name,
arguments, outcome, a short preview), so a later turn can be replayed to the
model with what the earlier one did. NULL for a human turn, a run without tool
calls and every row written before this.

`chat_message.restricted` is true when the run read restricted data: such an
answer is never replayed to a model that may not see it.

Revision ID: 0133
Revises: 0132
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0133"
down_revision: str | None = "0132"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS work_digest text")
    op.execute(
        "ALTER TABLE chat_message "
        "ADD COLUMN IF NOT EXISTS restricted boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS restricted")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS work_digest")
