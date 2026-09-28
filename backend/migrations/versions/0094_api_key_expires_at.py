"""api_key.expires_at -- optional per-key expiry, set by the member who
creates the key (not a tenant-wide policy).

Revision ID: 0094
Revises: 0093
Create Date: 2026-09-15

NOTE: this worktree has no `api_key` table or ApiKey model yet -- this
revision was backported ahead of the migration/model that actually creates
it. Guarded on table existence so a from-scratch chain replay (fresh
testcontainer) no-ops here instead of failing with "relation api_key does
not exist"; once the real creating migration lands, this guard becomes a
no-op everywhere and can be dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0094"
down_revision: str | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("api_key"):
        op.add_column("api_key", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("api_key"):
        op.drop_column("api_key", "expires_at")
