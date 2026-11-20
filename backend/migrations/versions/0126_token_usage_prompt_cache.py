"""Add token_usage_record's prompt-cache split.

`cache_read_tokens` and `cache_write_tokens` are the parts of `tokens_in` the
provider read from, or wrote to, its prompt cache. `tokens_in` stays the whole
prompt, so budgets and existing totals keep their meaning; cost bills the two
parts at the provider's cache rates. `token_usage_record` is created live from
the model in 0001_initial.py, hence IF NOT EXISTS / IF EXISTS (as in 0056).

Revision ID: 0126
Revises: 0125
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0126"
down_revision: str | None = "0125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE token_usage_record ADD COLUMN IF NOT EXISTS "
        "cache_read_tokens bigint NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE token_usage_record ADD COLUMN IF NOT EXISTS "
        "cache_write_tokens bigint NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE token_usage_record DROP COLUMN IF EXISTS cache_write_tokens")
    op.execute("ALTER TABLE token_usage_record DROP COLUMN IF EXISTS cache_read_tokens")
