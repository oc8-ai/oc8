"""Columns for split confirmations, a held department frame and confirmed skills.

`agent_tool_access.binding_split` and `trigger.access_split` hold the split
(`core` and `grant`) of what a person confirmed with a binding or trigger
(plan Rulings 1, 2): every stamp records one; NULL is a row stamped before the
split existed, judged by its digest alone. `department.pending_frame_tools`
holds the tool policy of a personal department that is waiting for its owner
to confirm it (Ruling 3); only a personal department can carry one
(`ck_department_pending_frame_owned`). `agent.confirmed_skills` holds the
skill assignments the owner of an agent's department last confirmed, or the
set the agent was created with
(`{assignment id: [skill_version_id, overrides, name, description]}`), so the
skills keep waiting for the owner when a confirmation goes away (Ruling 4,
Rulings T5-I1 and FR-I2).

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
    op.execute("ALTER TABLE agent ADD COLUMN IF NOT EXISTS confirmed_skills jsonb")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_pending_frame_owned")
    op.execute(
        "ALTER TABLE department ADD CONSTRAINT ck_department_pending_frame_owned "
        "CHECK (pending_frame_tools IS NULL OR owner_member_id IS NOT NULL)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_pending_frame_owned")
    op.execute("ALTER TABLE agent DROP COLUMN IF EXISTS confirmed_skills")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS pending_frame_tools")
    op.execute("ALTER TABLE trigger DROP COLUMN IF EXISTS access_split")
    op.execute("ALTER TABLE agent_tool_access DROP COLUMN IF EXISTS binding_split")
