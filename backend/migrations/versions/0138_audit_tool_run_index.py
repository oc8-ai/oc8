"""Find a run's or a chat's tool calls in the audit log (spec §5.7).

Tool events now name `run_id` and, for a chat run, `chat_session_id` in
`resource`; `GET /audit` filters every event on both (not only tool events, so
the indexes are not partial). Two expression indexes keep that lookup off a
scan of the whole log. Both are built
CONCURRENTLY outside the migration transaction, without a statement timeout
(the log only grows), so writers are never blocked.

Revision ID: 0138
Revises: 0137
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from oc8.models.ops import AuditEvent

revision: str = "0138"
down_revision: str | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = AuditEvent.__tablename__
_INDEXES = {
    "ix_audit_event_tool_run": "run_id",
    "ix_audit_event_tool_chat": "chat_session_id",
}


def _build_index_concurrently(name: str, ddl: str) -> None:
    """`ddl` (a `CREATE INDEX CONCURRENTLY IF NOT EXISTS`) outside the
    migration transaction, after dropping an INVALID leftover of a failed
    earlier build that `IF NOT EXISTS` would otherwise keep for good. Same as
    `0111_chat_c1.py` and `0137`, with no `statement_timeout` for the build (a
    session setting here, outside any transaction, so reset afterwards)."""
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
                    {"name": name},
                )
                .first()
            )
            if invalid is not None:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(ddl)
        finally:
            op.execute("RESET statement_timeout")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, key in _INDEXES.items():
        _build_index_concurrently(
            name,
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {_TABLE} ((resource->>'{key}'))",
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
