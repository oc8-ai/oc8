"""Flows can be archived and flow runs cancelled.

`flow.archived_at` hides a flow from the list and stops new runs of it while
the runs already going keep their history. `flow_run.status` gains
`cancelled` for a run somebody stopped.

Revision ID: 0118
Revises: 0117
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0118"
down_revision: str | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("flow", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_flow_run_status", "flow_run", type_="check")
    op.create_check_constraint(
        "ck_flow_run_status",
        "flow_run",
        "status IN ('running','completed','failed','suspended','cancelled')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE flow_run SET status = 'failed' WHERE status = 'cancelled'")
    op.drop_constraint("ck_flow_run_status", "flow_run", type_="check")
    op.create_check_constraint(
        "ck_flow_run_status",
        "flow_run",
        "status IN ('running','completed','failed','suspended')",
    )
    op.drop_column("flow", "archived_at")
