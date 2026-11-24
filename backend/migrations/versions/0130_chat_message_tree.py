"""Chat messages form a tree.

`chat_message.parent_id` links a message to the one it follows, so an edited
question or a regenerated answer becomes a sibling branch instead of a row
appended to one line. `chat_session.current_leaf_id` is the tip of the branch
the person is looking at. Both stay NULL for `kind='live'` chats, which remain
linear.

Existing `kind='chat'` sessions are backfilled in `id` order (uuid7, i.e.
creation order). Per answered question the "main variant" is the newest
variant when all variants sit side by side, else the first; the main line is
every row except the other variants, each following the previous main-line
row. The other variants hang off the main variant's parent as side branches.
The leaf is the last main-line row, so nothing a person saw goes off-path.

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
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS current_leaf_id uuid NULL")
    op.execute("""
        WITH base AS (
            SELECT m.id, m.session_id, m.role, m.reply_to_id
            FROM chat_message m
            JOIN chat_session s ON s.id = m.session_id AND s.kind = 'chat'
        ),
        grp AS (
            SELECT b.id, b.session_id,
                   (b.role = 'assistant' AND b.reply_to_id IS NOT NULL) AS grouped,
                   first_value(b.id) OVER w AS first_id,
                   last_value(b.id) OVER w AS last_id,
                   count(*) OVER w AS size
            FROM base b
            WINDOW w AS (
                PARTITION BY b.session_id, b.role, b.reply_to_id ORDER BY b.id
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
            )
        ),
        picked AS (
            -- The main variant of a regenerated answer is the newest one when the
            -- variants sit side by side, else the first: nothing the person saw
            -- may drop off the path.
            SELECT g.id, g.session_id,
                   CASE WHEN NOT g.grouped THEN g.id
                        WHEN (SELECT count(*) FROM base x
                              WHERE x.session_id = g.session_id
                                AND x.id BETWEEN g.first_id AND g.last_id) = g.size
                        THEN g.last_id
                        ELSE g.first_id END AS main_id
            FROM grp g
        ),
        main_line AS (
            SELECT p.id, p.session_id,
                   lag(p.id) OVER (PARTITION BY p.session_id ORDER BY p.id) AS prev_id
            FROM picked p
            WHERE p.id = p.main_id
        ),
        linked AS (
            UPDATE chat_message cm
            SET parent_id = ml.prev_id
            FROM picked p
            JOIN main_line ml ON ml.id = p.main_id
            WHERE cm.id = p.id
            RETURNING cm.id
        )
        UPDATE chat_session s
        SET current_leaf_id = (
            SELECT ml.id FROM main_line ml
            WHERE ml.session_id = s.id
            ORDER BY ml.id DESC LIMIT 1
        )
        WHERE s.kind = 'chat'
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_message_parent_id ON chat_message (parent_id)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_chat_message_parent_id")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS parent_id")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS current_leaf_id")
