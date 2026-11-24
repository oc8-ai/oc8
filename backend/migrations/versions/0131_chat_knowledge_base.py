"""A knowledge base can belong to one chat.

`knowledge_base.kind` is `'shared'` for every base the organisation manages
(all existing rows, via the column default) and `'chat'` for the hidden base
that holds the large files of one chat session, named by
`knowledge_base.chat_session_id`. A chat has at most one live base of its own;
a deleted one does not count, so a chat can get a fresh base after its old one
was dropped.

Revision ID: 0131
Revises: 0130
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0131"
down_revision: str | None = "0130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'shared'"
    )
    op.execute("ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS chat_session_id uuid NULL")
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_knowledge_base_kind'
            ) THEN
                ALTER TABLE knowledge_base
                    ADD CONSTRAINT ck_knowledge_base_kind CHECK (kind IN ('shared','chat'));
            END IF;
        END
        $$
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_base_chat_session "
        "ON knowledge_base (tenant_id, chat_session_id) "
        "WHERE chat_session_id IS NOT NULL AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_chat_session_id "
        "ON knowledge_base (chat_session_id)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Without `kind` a chat's own base would read as one the organisation
    # manages -- listed, grantable, searchable. Retire every chat base first:
    # its chunks go (they are the chat's files), its `chat_upload` source and
    # the base itself are soft-deleted. Skipped once the column is gone.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'knowledge_base' AND column_name = 'kind'
            ) THEN
                DELETE FROM kb_chunk
                WHERE kb_id IN (SELECT id FROM knowledge_base WHERE kind = 'chat');
                UPDATE data_source SET deleted_at = now(), updated_at = now()
                WHERE connector_type = 'chat_upload'
                  AND deleted_at IS NULL
                  AND id::text IN (
                      SELECT jsonb_array_elements_text(source_ids)
                      FROM knowledge_base WHERE kind = 'chat'
                  );
                UPDATE knowledge_base SET deleted_at = now(), updated_at = now()
                WHERE kind = 'chat' AND deleted_at IS NULL;
            END IF;
        END
        $$
    """)
    op.execute("DROP INDEX IF EXISTS ix_knowledge_base_chat_session_id")
    op.execute("DROP INDEX IF EXISTS uq_knowledge_base_chat_session")
    op.execute("ALTER TABLE knowledge_base DROP CONSTRAINT IF EXISTS ck_knowledge_base_kind")
    op.execute("ALTER TABLE knowledge_base DROP COLUMN IF EXISTS chat_session_id")
    op.execute("ALTER TABLE knowledge_base DROP COLUMN IF EXISTS kind")
