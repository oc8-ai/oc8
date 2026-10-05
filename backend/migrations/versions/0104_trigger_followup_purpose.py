"""Add trigger.followup_purpose (Copilot proactive research, D2).

`check_in` or `research`; NULL is a follow-up from before this column and reads
as `check_in`. Deliberately no CHECK constraint, like chat_message.mode: the
application fails an unknown value CLOSED (it fires as research), and a newer
release's value must not be rejected by an older database.

Revision ID: 0104
Revises: 0103
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0104"
down_revision: str | None = "0103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE trigger ADD COLUMN IF NOT EXISTS followup_purpose text")


def downgrade() -> None:
    op.execute("ALTER TABLE trigger DROP COLUMN IF EXISTS followup_purpose")
