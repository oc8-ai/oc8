"""Give department the same Copilot-managed config_revision column
migration 0054 gave agent/plugin/integration.

Revision ID: 0096
Revises: 0095
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE department ADD COLUMN IF NOT EXISTS "
        "config_revision integer NOT NULL DEFAULT 1"
    )
    # oc8_bump_copilot_config_revision() already exists (migration 0054);
    # department just needs its own trigger wired to the same function.
    op.execute("DROP TRIGGER IF EXISTS trg_department_copilot_config_revision ON department")
    op.execute(
        "CREATE TRIGGER trg_department_copilot_config_revision BEFORE UPDATE ON department "
        "FOR EACH ROW EXECUTE FUNCTION oc8_bump_copilot_config_revision()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_department_copilot_config_revision ON department")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS config_revision")
