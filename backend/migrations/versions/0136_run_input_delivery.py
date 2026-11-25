"""Steer or queue: how a message written to a running run is delivered.

`run_input.delivery` is `'steer'` (today's behaviour, the default: the run
reads it at its next step boundary) or `'next_turn'` (queued: the run never
reads it; it becomes the chat's next turn when the run ends). Consumers read a
run's waiting inputs of one delivery, so the pending index leads with it.

Revision ID: 0136
Revises: 0135
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0136"
down_revision: str | None = "0135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE run_input ADD COLUMN IF NOT EXISTS delivery text NOT NULL DEFAULT 'steer'"
    )
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE run_input ADD CONSTRAINT ck_run_input_delivery
                CHECK (delivery IN ('steer', 'next_turn'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("DROP INDEX IF EXISTS ix_run_input_run_pending")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_input_run_pending "
        "ON run_input (run_id, delivery, consumed_at)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_run_input_run_pending")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_input_run_pending ON run_input (run_id, consumed_at)"
    )
    op.execute("ALTER TABLE run_input DROP CONSTRAINT IF EXISTS ck_run_input_delivery")
    op.execute("ALTER TABLE run_input DROP COLUMN IF EXISTS delivery")
