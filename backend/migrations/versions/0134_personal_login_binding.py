"""Columns for split confirmations and a held department frame.

`agent_tool_access.binding_split` and `trigger.access_split` hold the policy
snapshot a person confirmed when a binding or trigger acts through more than
one account (Ruling 3). `department.pending_frame_tools` holds the tool policy
of a personal department that is waiting for its owner to confirm it (Ruling
1); only a personal department can carry one
(`ck_department_pending_frame_owned`).

Everything is idempotent: `0001` runs `create_all` from the live models, so a
fresh database already has these columns and the constraint.

Revision ID: 0134
Revises: 0133
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0134"
down_revision: str | None = "0133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE agent_tool_access ADD COLUMN IF NOT EXISTS binding_split jsonb")
    op.execute("ALTER TABLE trigger ADD COLUMN IF NOT EXISTS access_split jsonb")
    op.execute("ALTER TABLE department ADD COLUMN IF NOT EXISTS pending_frame_tools jsonb")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_pending_frame_owned")
    op.execute(
        "ALTER TABLE department ADD CONSTRAINT ck_department_pending_frame_owned "
        "CHECK (pending_frame_tools IS NULL OR owner_member_id IS NOT NULL)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_pending_frame_owned")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS pending_frame_tools")
    op.execute("ALTER TABLE trigger DROP COLUMN IF EXISTS access_split")
    op.execute("ALTER TABLE agent_tool_access DROP COLUMN IF EXISTS binding_split")
