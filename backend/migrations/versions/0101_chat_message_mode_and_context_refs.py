"""Add ChatMessage.mode and ChatMessage.context_refs.

`mode` is the slash command a turn was sent in -- ask | plan | do | summarise,
NULL for an ordinary message (design doc
2026-09-27-ai-workplace-collaboration-design.md §5.2). Stored on the message
rather than derived from its text, because the command is stripped out of
`content`: the transcript shows what was said, the badge shows how it was
asked.

`context_refs` is what the operator attached with `#` for that one turn --
`[{"kind": "knowledge_base", "id": "...", "label": "..."}]`, resolved and
labelled at send time so a renamed or deleted knowledge base does not make an
old turn unreadable. Empty list for every message that attached nothing.

Deliberately no CHECK constraint on `mode`: the set of modes is application
vocabulary that changes with a release, and a run enqueued by a newer version
must not be rejected by an older database. `mode_from_context` fails open on
an unknown key for the same reason.

IF NOT EXISTS: `chat_message` is created by 0001_initial.py's live
Base.metadata.create_all(), so both columns already exist on a fresh database
by the time 0001 finishes.

Revision ID: 0101
Revises: 0100
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0101"
down_revision: str | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS mode text")
    op.execute(
        "ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS context_refs jsonb "
        "NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS context_refs")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS mode")
