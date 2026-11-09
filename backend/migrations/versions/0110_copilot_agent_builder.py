"""Copilot Agent Builder: builder proposals and a chat's sticky mode.

Revision ID: 0110
Revises: 0109
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0110"
down_revision: str | None = "0109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Fail fast rather than queue the app behind a run's transaction (0108).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE copilot_proposal ADD COLUMN IF NOT EXISTS kind text "
        "NOT NULL DEFAULT 'operations'"
    )
    op.execute("ALTER TABLE copilot_proposal ADD COLUMN IF NOT EXISTS session_id uuid")
    op.execute("ALTER TABLE copilot_proposal ADD COLUMN IF NOT EXISTS expires_at timestamptz")
    op.execute(
        "ALTER TABLE copilot_proposal ADD COLUMN IF NOT EXISTS result jsonb "
        "NOT NULL DEFAULT '{}'::jsonb"
    )
    # The status vocabulary grows; the table is small, so the re-check is cheap.
    op.execute("ALTER TABLE copilot_proposal DROP CONSTRAINT IF EXISTS ck_copilot_proposal_status")
    op.execute(
        "ALTER TABLE copilot_proposal ADD CONSTRAINT ck_copilot_proposal_status CHECK "
        "(status IN ('draft','applied','expired','rejected','awaiting_approval','superseded'))"
    )
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE copilot_proposal ADD CONSTRAINT ck_copilot_proposal_kind
                CHECK (kind IN ('operations','agent_builder'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_copilot_proposal_session_status "
        "ON copilot_proposal (session_id, status)"
    )
    # "One open builder proposal per chat session" (spec 3.2), held by the
    # database rather than by every caller remembering to supersede first.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_copilot_proposal_open_builder "
        "ON copilot_proposal (session_id) "
        "WHERE kind = 'agent_builder' AND status IN ('draft','awaiting_approval')"
    )
    # No CHECK, like chat_message.mode: the mode vocabulary is application-level.
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS mode text")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS mode")
    op.execute("DROP INDEX IF EXISTS uq_copilot_proposal_open_builder")
    op.execute("DROP INDEX IF EXISTS ix_copilot_proposal_session_status")
    op.execute("ALTER TABLE copilot_proposal DROP CONSTRAINT IF EXISTS ck_copilot_proposal_kind")
    # Rows in the two new statuses cannot survive the old CHECK.
    op.execute(
        "UPDATE copilot_proposal SET status = 'expired' "
        "WHERE status IN ('awaiting_approval','superseded')"
    )
    op.execute("ALTER TABLE copilot_proposal DROP CONSTRAINT IF EXISTS ck_copilot_proposal_status")
    op.execute(
        "ALTER TABLE copilot_proposal ADD CONSTRAINT ck_copilot_proposal_status CHECK "
        "(status IN ('draft','applied','expired','rejected'))"
    )
    op.execute("ALTER TABLE copilot_proposal DROP COLUMN IF EXISTS result")
    op.execute("ALTER TABLE copilot_proposal DROP COLUMN IF EXISTS expires_at")
    op.execute("ALTER TABLE copilot_proposal DROP COLUMN IF EXISTS session_id")
    op.execute("ALTER TABLE copilot_proposal DROP COLUMN IF EXISTS kind")
