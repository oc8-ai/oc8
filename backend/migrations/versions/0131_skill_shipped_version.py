"""Edit shipped skills in place.

`skill.shipped_version_id` names the version holding the content as it was
shipped (by a capa, or imported from a store): what a reset restores and what a
shipped update compares against. `skill.customized_at` records that an admin
changed that content in place; `skill.shipped_update_at` that a newer shipped
version arrived for a customised skill and was kept aside instead of replacing
the edit.

Every existing store skill is backfilled with its current version as the
shipped one: until now a store skill could only be changed by duplicating it.

`skill` already has row-level security and the app role's grants (0001); new
columns inherit both.

Revision ID: 0131
Revises: 0129
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0131"
down_revision: str | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE skill ADD COLUMN IF NOT EXISTS shipped_version_id uuid")
    op.execute("ALTER TABLE skill ADD COLUMN IF NOT EXISTS customized_at timestamptz")
    op.execute("ALTER TABLE skill ADD COLUMN IF NOT EXISTS shipped_update_at timestamptz")
    op.execute(
        "UPDATE skill SET shipped_version_id = current_version_id "
        "WHERE origin = 'store' AND shipped_version_id IS NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE skill DROP COLUMN IF EXISTS shipped_update_at")
    op.execute("ALTER TABLE skill DROP COLUMN IF EXISTS customized_at")
    op.execute("ALTER TABLE skill DROP COLUMN IF EXISTS shipped_version_id")
