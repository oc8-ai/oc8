"""Agent tool access: consent for a fixed binding to a personal account.

`set_with_manage` records that an administrator set the binding;
`agent_revision` is the agent's config_revision when it was confirmed. A
personal target is only used while both the consent basis holds and the agent
is unchanged since then.

Revision ID: 0109
Revises: 0108
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0109"
down_revision: str | None = "0108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def add_consent_columns(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "ALTER TABLE agent_tool_access "
            "ADD COLUMN IF NOT EXISTS set_with_manage boolean NOT NULL DEFAULT false, "
            "ADD COLUMN IF NOT EXISTS agent_revision integer"
        )
    )


def remove_consent_columns(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "ALTER TABLE agent_tool_access DROP COLUMN IF EXISTS set_with_manage, "
            "DROP COLUMN IF EXISTS agent_revision"
        )
    )


def upgrade() -> None:
    add_consent_columns(op.get_bind())


def downgrade() -> None:
    remove_consent_columns(op.get_bind())
