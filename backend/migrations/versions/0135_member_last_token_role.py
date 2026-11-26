"""`org_member` remembers the role claim of the last token it was seen with.

`last_token_role` and `last_token_role_at` are copied by the gate's upsert
(`authz.scope._upsert_member`) only when the claim changes. A run with no token
of its member behind it -- a personal agent's trigger run borrowing knowledge
from the owner's departments (spec A5, ruling U4) -- reads it for a member with
no assigned role. NULL is a member not seen with a token since this migration.

Idempotent: `0001` runs `create_all` from the live models, so a fresh database
already has the columns.

Revision ID: 0135
Revises: 0134
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0135"
down_revision: str | None = "0134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE org_member ADD COLUMN IF NOT EXISTS last_token_role text")
    op.execute("ALTER TABLE org_member ADD COLUMN IF NOT EXISTS last_token_role_at timestamptz")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE org_member DROP COLUMN IF EXISTS last_token_role_at")
    op.execute("ALTER TABLE org_member DROP COLUMN IF EXISTS last_token_role")
