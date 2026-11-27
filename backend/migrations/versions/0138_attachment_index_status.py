"""A chat file knows whether its text was indexed.

`file_attachment.index_status` is NULL for every row that is not a chat file
(and for every row written before this), `'inline'` for a chat file small
enough to hand to the model whole, `'queued'` while a large one waits for the
background ingest, `'indexed'` once it is searchable in the chat's own
knowledge base, and `'failed'` when it could not be indexed and is handed to
the model inline and truncated instead.

`file_attachment.indexed_from_id` names the attachment whose indexed text a
row shares: an edited turn's copy of a file is not ingested a second time.

Revision ID: 0138
Revises: 0137
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0138"
down_revision: str | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE file_attachment ADD COLUMN IF NOT EXISTS index_status text NULL")
    op.execute("ALTER TABLE file_attachment ADD COLUMN IF NOT EXISTS indexed_from_id uuid NULL")
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_file_attachment_index_status'
            ) THEN
                ALTER TABLE file_attachment
                    ADD CONSTRAINT ck_file_attachment_index_status CHECK (
                        index_status IS NULL
                        OR index_status IN ('queued','indexed','failed','inline')
                    );
            END IF;
        END
        $$
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_file_attachment_indexed_from_id "
        "ON file_attachment (indexed_from_id) WHERE indexed_from_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_file_attachment_indexed_from_id")
    op.execute(
        "ALTER TABLE file_attachment DROP CONSTRAINT IF EXISTS ck_file_attachment_index_status"
    )
    op.execute("ALTER TABLE file_attachment DROP COLUMN IF EXISTS indexed_from_id")
    op.execute("ALTER TABLE file_attachment DROP COLUMN IF EXISTS index_status")
