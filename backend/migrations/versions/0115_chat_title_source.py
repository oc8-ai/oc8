"""Chat titles: record where a chat's title came from.

`title_source` is NULL while a chat has no title, 'auto' once one was
generated from its first message, 'user' once the member named it. A
generated title only ever replaces NULL, so a member's own name is never
overwritten. Every title that exists before this revision was typed by a
person (nothing else wrote one), so those become 'user'.

Revision ID: 0115
Revises: 0114
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0115"
down_revision: str | None = "0114"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Fail fast rather than queue the app behind a run's transaction (0112).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS title_source text")
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE chat_session ADD CONSTRAINT ck_chat_session_title_source
                CHECK (title_source IS NULL OR title_source IN ('auto','user'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("UPDATE chat_session SET title_source = 'user' WHERE title <> ''")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_session DROP CONSTRAINT IF EXISTS ck_chat_session_title_source")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS title_source")
