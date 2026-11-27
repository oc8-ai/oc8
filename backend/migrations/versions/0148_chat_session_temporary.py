"""Temporary chats (chat experience phase 1c, spec §5.6).

`chat_session.temporary` marks a chat that is not kept: never listed, never
named or summarised, and deleted when the person leaves it or, at the latest,
by the worker's purge 24 hours after its last message. NOT NULL DEFAULT
false: every existing chat is an ordinary one. A constant default is a
catalogue-only change on Postgres 11+, no rewrite.

The partial index serves only the purge's scan for temporary chats, which
are few at any time; ordinary chats never enter it.

Revision ID: 0148
Revises: 0147
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0148"
down_revision: str | None = "0147"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Literals, never the model's names: a later rename must not change what this
# historical revision does.
_TABLE = "chat_session"
_INDEX = "ix_chat_session_temporary"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS temporary boolean NOT NULL DEFAULT false"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (tenant_id, last_message_at) "
        "WHERE temporary"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS temporary")
