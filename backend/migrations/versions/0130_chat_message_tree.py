"""Chat messages form a tree.

`chat_message.parent_id` links a message to the one it follows, so an edited
question or a regenerated answer becomes a sibling branch instead of a row
appended to one line. `chat_session.current_leaf_id` is the tip of the branch
the person is looking at. Both stay NULL for `kind='live'` chats, which remain
linear.

Existing `kind='chat'` sessions are backfilled in `id` order (uuid7, i.e.
creation order): every message follows the previous one, except a regenerated
variant (a later assistant message answering the same `reply_to_id`), which
shares the parent of the first variant. The message after a variant group
follows the newest variant. The leaf is the last message.

Revision ID: 0130
Revises: 0129
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0130"
down_revision: str | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS parent_id uuid NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_message_parent_id ON chat_message (parent_id)")
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS current_leaf_id uuid NULL")
    op.execute("""
        WITH ordered AS (
            SELECT m.id, m.session_id, m.role, m.reply_to_id,
                   lag(m.id) OVER w AS prev_id
            FROM chat_message m
            JOIN chat_session s ON s.id = m.session_id AND s.kind = 'chat'
            WINDOW w AS (PARTITION BY m.session_id ORDER BY m.id)
        ),
        variants AS (
            SELECT o.*,
                   CASE WHEN o.role = 'assistant' AND o.reply_to_id IS NOT NULL
                        THEN first_value(o.prev_id) OVER (
                            PARTITION BY o.session_id, o.reply_to_id
                            ORDER BY o.id
                        ) END AS first_variant_parent,
                   CASE WHEN o.role = 'assistant' AND o.reply_to_id IS NOT NULL
                        THEN row_number() OVER (
                            PARTITION BY o.session_id, o.reply_to_id
                            ORDER BY o.id
                        ) END AS variant_no
            FROM ordered o
            WHERE true
        )
        UPDATE chat_message cm
        SET parent_id = CASE WHEN v.variant_no > 1 THEN v.first_variant_parent ELSE v.prev_id END
        FROM variants v
        WHERE cm.id = v.id
    """)
    op.execute("""
        UPDATE chat_session s
        SET current_leaf_id = (
            SELECT m.id FROM chat_message m
            WHERE m.session_id = s.id
            ORDER BY m.id DESC LIMIT 1
        )
        WHERE s.kind = 'chat'
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_chat_message_parent_id")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS parent_id")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS current_leaf_id")
