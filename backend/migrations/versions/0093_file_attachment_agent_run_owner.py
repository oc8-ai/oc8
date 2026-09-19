"""file_attachment gains a third owner_type: agent_run

An agent run producing a file (via write_output_file or a synced
/workspace/output/ mount) needs somewhere to store it. FileAttachment
already has everything -- tenant scoping, RLS, S3 key, extracted text --
so this just widens the existing owner_type constraint rather than adding
a new table. owner_id for these rows is AgentRun.id.

Revision ID: 0093
Revises: 0092
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = "'chat_message','agent_instructions'"
_NEW_TYPES = "'chat_message','agent_instructions','agent_run'"


def upgrade() -> None:
    op.drop_constraint("ck_file_attachment_owner_type", "file_attachment", type_="check")
    op.create_check_constraint(
        "ck_file_attachment_owner_type", "file_attachment", f"owner_type IN ({_NEW_TYPES})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_file_attachment_owner_type", "file_attachment", type_="check")
    op.create_check_constraint(
        "ck_file_attachment_owner_type", "file_attachment", f"owner_type IN ({_OLD_TYPES})"
    )
