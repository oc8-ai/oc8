"""Count answer feedback without scanning the audit log.

`GET /stats/feedback` reads the `chat.feedback` events of one tenant in a
period. A partial index on (tenant_id, ts) holds only those events, so the
count stays off a scan of the whole log. Built CONCURRENTLY outside the
migration transaction, without a statement timeout (the log only grows), so
writers are never blocked -- as in 0144.

Revision ID: 0146
Revises: 0145
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0146"
down_revision: str | None = "0145"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Literals, never the model's names: a later rename must not change what this
# historical revision does.
_INDEX = "ix_audit_event_feedback_tenant_ts"
_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
    "ON audit_event (tenant_id, ts) WHERE action = 'chat.feedback'"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Same as 0144: drop an INVALID leftover of a failed earlier build, which
    # `IF NOT EXISTS` would otherwise keep for good.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        try:
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "WHERE c.relname = :name AND NOT i.indisvalid"
                    ),
                    {"name": _INDEX},
                )
                .first()
            )
            if invalid is not None:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
            op.execute(_DDL)
        finally:
            op.execute("RESET statement_timeout")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
