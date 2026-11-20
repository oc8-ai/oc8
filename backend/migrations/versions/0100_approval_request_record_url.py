"""Add ApprovalRequest.record_url.

The resolved deep link to the record an approval is about (design doc
2026-09-27-ai-workplace-collaboration-design.md §6): an approver reading
"Nora wants to create a quotation for EUR 4,200" had to go and find that
quotation themselves. Resolved by the gateway at raise time from the tool
pack's own `record_url` template, so core stays vendor-neutral and a pack
that declares nothing produces NULL -- which is exactly today's behaviour.

Nullable and additive: every existing row keeps NULL and every approval view
renders as it did.

IF NOT EXISTS: `approval_request` is one of the tables 0001_initial.py creates
via a live Base.metadata.create_all(), so this column already exists on a
fresh database by the time 0001 finishes.

Revision ID: 0100
Revises: 0099
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0100"
down_revision: str | None = "0099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE approval_request ADD COLUMN IF NOT EXISTS record_url text")


def downgrade() -> None:
    op.execute("ALTER TABLE approval_request DROP COLUMN IF EXISTS record_url")
