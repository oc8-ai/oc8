"""oc8 Chat C1: per-chat model override, human author per turn, reply link.

Additive only (spec §9): nullable columns plus `model_config.chat_enabled`
(NOT NULL, default true). `reply_to_id` points an assistant turn at the user
turn it answers, so "Regenerate" can keep both answers ("1/2") and two
pending turns never swap replies. `parts` holds typed message parts.

Revision ID: 0107
Revises: 0106
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0107"
down_revision: str | None = "0106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS model_override_id uuid")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS author_member_id uuid")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS reply_to_id uuid")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS parts jsonb")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_message_reply_to_id ON chat_message (reply_to_id)"
    )
    op.execute(
        "ALTER TABLE model_config ADD COLUMN IF NOT EXISTS chat_enabled boolean "
        "NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE model_config DROP COLUMN IF EXISTS chat_enabled")
    op.execute("DROP INDEX IF EXISTS ix_chat_message_reply_to_id")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS parts")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS reply_to_id")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS author_member_id")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS model_override_id")
