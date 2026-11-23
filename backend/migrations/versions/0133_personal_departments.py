"""A department has a kind, an owner and override stamps.

`department.kind` (`standard`, `assistant`, `personal`) folds the old
`is_assistant_department` flag in: the assistant's department becomes
`kind = 'assistant'` and the flag is dropped. A `personal` department belongs
to one member (`owner_member_id`, set exactly for that kind) and a member has
at most one live one.

`frame_overridden_at` and `budget_overridden_at` are stamped when someone edits
a personal department's tools or budget by hand, so a re-sync of the tenant
defaults skips that part.

Everything is idempotent: `0001` runs `create_all` from the live models, so a
fresh database already has these columns, constraints and index. Downgrade
soft-deletes personal departments before dropping the columns, so a downgraded
database never turns a private department into an ordinary visible one.

Revision ID: 0133
Revises: 0132
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0133"
down_revision: str | None = "0132"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE department ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'standard'"
    )
    op.execute("ALTER TABLE department ADD COLUMN IF NOT EXISTS owner_member_id uuid")
    op.execute("ALTER TABLE department ADD COLUMN IF NOT EXISTS frame_overridden_at timestamptz")
    op.execute("ALTER TABLE department ADD COLUMN IF NOT EXISTS budget_overridden_at timestamptz")
    # The flag only exists on a database that came up through 0083.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'department' AND column_name = 'is_assistant_department'
            ) THEN
                UPDATE department SET kind = 'assistant' WHERE is_assistant_department;
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS is_assistant_department")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_kind")
    op.execute(
        "ALTER TABLE department ADD CONSTRAINT ck_department_kind "
        "CHECK (kind IN ('standard','assistant','personal'))"
    )
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_personal_owner")
    op.execute(
        "ALTER TABLE department ADD CONSTRAINT ck_department_personal_owner "
        "CHECK ((kind = 'personal') = (owner_member_id IS NOT NULL))"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_department_personal_owner "
        "ON department (tenant_id, owner_member_id) "
        "WHERE kind = 'personal' AND deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE department SET deleted_at = now() WHERE kind = 'personal' AND deleted_at IS NULL"
    )
    op.execute(
        "ALTER TABLE department ADD COLUMN IF NOT EXISTS "
        "is_assistant_department boolean NOT NULL DEFAULT false"
    )
    op.execute("UPDATE department SET is_assistant_department = (kind = 'assistant')")
    op.execute("DROP INDEX IF EXISTS uq_department_personal_owner")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_personal_owner")
    op.execute("ALTER TABLE department DROP CONSTRAINT IF EXISTS ck_department_kind")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS budget_overridden_at")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS frame_overridden_at")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS owner_member_id")
    op.execute("ALTER TABLE department DROP COLUMN IF EXISTS kind")
