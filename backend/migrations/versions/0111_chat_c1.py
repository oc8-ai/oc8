"""oc8 Chat C1: per-chat model override, human author per turn, reply link.

Additive only (spec §9): nullable columns plus `model_config.chat_enabled`
(NOT NULL, default true). `reply_to_id` points an assistant turn at the user
turn it answers, so "Regenerate" can keep both answers ("1/2") and two
pending turns never swap replies. `parts` holds typed message parts.

Production-safe: `lock_timeout` makes the ALTERs fail fast rather than queue
the app behind a long-running run's transaction, and the `chat_message` index
is built CONCURRENTLY outside the transaction (see the PR's migration notes).

Revision ID: 0111
Revises: 0110
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0111"
down_revision: str | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Fail a blocked DDL fast instead of queueing every later query behind it
#: (a run's transaction can hold these tables for minutes). Retry the deploy.
LOCK_TIMEOUT: Final = "5s"


def _build_index_concurrently(name: str, ddl: str) -> None:
    """`ddl` (a `CREATE INDEX CONCURRENTLY IF NOT EXISTS`) outside the
    migration transaction, so writes to the table go on while it builds. A
    failed earlier build leaves an INVALID index that `IF NOT EXISTS` would
    keep for good, so drop such a leftover first. Committing here also ends
    the transaction holding the earlier ALTERs' locks."""
    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": name},
            )
            .first()
        )
        if invalid is not None:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(ddl)


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS model_override_id uuid")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS author_member_id uuid")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS reply_to_id uuid")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS parts jsonb")
    op.execute(
        "ALTER TABLE model_config ADD COLUMN IF NOT EXISTS chat_enabled boolean "
        "NOT NULL DEFAULT true"
    )
    _build_index_concurrently(
        "ix_chat_message_reply_to_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_message_reply_to_id "
        "ON chat_message (reply_to_id)",
    )


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute("ALTER TABLE model_config DROP COLUMN IF EXISTS chat_enabled")
    op.execute("DROP INDEX IF EXISTS ix_chat_message_reply_to_id")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS parts")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS reply_to_id")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS author_member_id")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS model_override_id")
