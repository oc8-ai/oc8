"""A long chat is summarised into checkpoints, replayed in place of its oldest turns.

`chat_checkpoint` holds one summary of a chat's path up to `upto_message_id`
(spec §5.3). A replay starts from the newest checkpoint on the path the person
is on and replays only the messages after it. `restricted` is true when the
summary was made from a restricted answer (by a local model): such a summary is
never replayed to a model that may not see it.

Revision ID: 0140
Revises: 0139
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0140"
down_revision: str | None = "0139"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_checkpoint (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            session_id uuid NOT NULL,
            upto_message_id uuid NOT NULL,
            summary text NOT NULL,
            token_estimate integer NOT NULL,
            model text NOT NULL,
            restricted boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE chat_checkpoint ADD CONSTRAINT fk_chat_checkpoint_session
                FOREIGN KEY (session_id) REFERENCES chat_session (id) ON DELETE CASCADE;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_checkpoint_tenant_id ON chat_checkpoint (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_checkpoint_session_created "
        "ON chat_checkpoint (session_id, created_at DESC)"
    )
    op.execute("ALTER TABLE chat_checkpoint ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON chat_checkpoint
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    # A database restored from a dump has no default privileges (0113).
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON chat_checkpoint TO oc8_app;
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS chat_checkpoint")
