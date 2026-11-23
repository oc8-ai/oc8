"""A polled trigger's baseline has an id.

`event_source_cursor.baseline_id` is drawn anew each time the poller takes a
baseline, and goes into the idempotency key of every event fired from it
together with the trigger's login. A source's event ids are only unique for
one login and one run of it (two mailboxes on one server number their mail
alike), so a trigger moved to another login, or started afresh, never takes a
new event for one it fired before.

A cursor that took its baseline before this revision has none, and keeps the
old key until its next baseline, so nothing it already fired fires again.

Revision ID: 0132
Revises: 0131
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0132"
down_revision: str | None = "0131"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE event_source_cursor ADD COLUMN IF NOT EXISTS baseline_id uuid")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE event_source_cursor DROP COLUMN IF EXISTS baseline_id")
