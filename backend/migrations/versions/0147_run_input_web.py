"""Keep a queued message's "Web" switch (chat experience phase 1c, spec §10).

A message sent while an answer still runs waits as a `next_turn` run input
and becomes the next turn when that run ends. The person's "Web" opt-in
belongs to the message, so `run_input.web` keeps it until then. NOT NULL
DEFAULT false: existing rows (and every `steer` input) never searched. A
constant default is a catalogue-only change on Postgres 11+, no rewrite.

Revision ID: 0147
Revises: 0146
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0147"
down_revision: str | None = "0146"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Literals, never the model's names: a later rename must not change what this
# historical revision does.
_TABLE = "run_input"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS web boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS web")
