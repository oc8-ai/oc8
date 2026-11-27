"""Record which member a token usage was spent for (Cost Center per person).

`token_usage_record.member_id` names the `org_member` a call was made for, so
the Cost Center can chart usage per employee over time. Nullable: rows from
before this revision, and calls made for nobody, stay NULL (no backfill).

No foreign key on purpose: usage records are immutable (the runtime role has
no UPDATE/DELETE, migration 0001) and must outlive the member they name, so
deleting a member must neither cascade into nor be blocked by its usage.

The `(tenant_id, ts) INCLUDE (member_id, tokens_in, tokens_out)` index serves
the per-member time series (`GET /usage/timeseries`). That query filters on
tenant and a recent `ts` range and groups by member, so `ts` must follow
`tenant_id` directly: with `member_id` in between, as an earlier draft of this
revision had it, Postgres 15 (no skip scan) cannot bound the range and reads
the tenant's whole history on every chart load. The INCLUDE columns let the
aggregate run as an index-only scan. It is built CONCURRENTLY outside the
migration transaction, so the completion hot path keeps inserting while it
builds.

The draft's `ix_token_usage_record_tenant_member_ts` was never released, but a
development database that applied the draft is already at this revision and will not
re-run this revision: drop it there by hand
(`DROP INDEX ix_token_usage_record_tenant_member_ts`) and create the new index
with the statement below, or rebuild the database. Upgrade drops the old name
IF EXISTS so a database that somehow carries it ends up with one index.

Revision ID: 0145
Revises: 0144
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0145"
down_revision: str | None = "0144"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Fail a blocked DDL fast instead of queueing every later insert behind it.
#: Retry the deploy.
LOCK_TIMEOUT: Final = "5s"

# Literals, never the model's names: a later rename of the model must not
# change what this historical revision does.
_TABLE = "token_usage_record"
_INDEX = "ix_token_usage_record_tenant_ts"
#: The unreleased draft's index (see the module docstring).
_DRAFT_INDEX = "ix_token_usage_record_tenant_member_ts"


def _build_index_concurrently(name: str, ddl: str) -> None:
    """`ddl` (a `CREATE INDEX CONCURRENTLY IF NOT EXISTS`) outside the
    migration transaction, after dropping an INVALID leftover of a failed
    earlier build that `IF NOT EXISTS` would otherwise keep for good. Same as
    `0111_chat_c1.py`; committing here also ends the transaction holding the
    ALTER's lock."""
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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS member_id uuid")
    _build_index_concurrently(
        _INDEX,
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON {_TABLE} (tenant_id, ts) "
        "INCLUDE (member_id, tokens_in, tokens_out)",
    )
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_DRAFT_INDEX}")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS member_id")
