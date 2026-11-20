"""oc8 Chat C4: the Live tab's session kind and the run_input queue.

`run_input` is its own table, not a key in `agent_run.context`: the executor
holds the run row's lock for the whole run, so a write there would block until
the run ends (same reason as run_cancellation / run_message).

Production-safe: `lock_timeout` makes the ALTERs fail fast rather than queue
the app behind a long-running run's transaction, the kind CHECK is added NOT
VALID and validated without blocking writes, and the `agent_run` index is
built CONCURRENTLY outside the transaction (see the PR's migration notes).

Revision ID: 0112
Revises: 0111
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0112"
down_revision: str | None = "0111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Fail a blocked DDL fast instead of queueing every later query behind it
#: (a run's transaction can hold these tables for minutes). Retry the deploy.
LOCK_TIMEOUT: Final = "5s"


def _build_index_concurrently(name: str, ddl: str) -> None:
    """`ddl` (a `CREATE INDEX CONCURRENTLY IF NOT EXISTS`) outside the
    migration transaction, so writes to the table go on while it builds. A
    failed earlier build leaves an INVALID index that `IF NOT EXISTS` would
    keep for good, so drop such a leftover first. Committing here also ends
    the transaction holding the earlier ALTERs' locks."""
    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": name},
            )
            .first()
        )
        if invalid is not None:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(ddl)


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(
        "ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'chat'"
    )
    # NOT VALID here, VALIDATE below in its own transaction: the scan then
    # runs under SHARE UPDATE EXCLUSIVE, not the ACCESS EXCLUSIVE of the ADD.
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE chat_session ADD CONSTRAINT ck_chat_session_kind
                CHECK (kind IN ('chat','live')) NOT VALID;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_session_live "
        "ON chat_session (tenant_id, member_id, agent_id) WHERE kind = 'live'"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS run_input (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            run_id uuid NOT NULL,
            seq bigint GENERATED ALWAYS AS IDENTITY,
            author_member_id uuid NOT NULL,
            text text NOT NULL,
            consumed_at timestamptz,
            consumed_step integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_run_input_text CHECK (char_length(text) BETWEEN 1 AND 4000)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_input_tenant_id ON run_input (tenant_id)")
    # Consumers read a run's pending inputs in `seq` order: acceptance order,
    # which `created_at` (transaction start) is not.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_input_run_pending ON run_input (run_id, consumed_at)"
    )
    op.execute("ALTER TABLE run_input ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON run_input
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    # Said out loud like 0046 does: a database restored from a dump has no
    # default privileges, and the runtime role would get `permission denied`.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON run_input TO oc8_app;
          END IF;
        END $$
    """)
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE chat_session VALIDATE CONSTRAINT ck_chat_session_kind")
    # The Live timeline pages an agent's runs newest first by (created_at, id).
    _build_index_concurrently(
        "ix_agent_run_agent_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_run_agent_created "
        "ON agent_run (agent_id, created_at DESC, id DESC)",
    )


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute("DROP TABLE IF EXISTS run_input")
    op.execute("DROP INDEX IF EXISTS ix_agent_run_agent_created")
    op.execute("DROP INDEX IF EXISTS uq_chat_session_live")
    op.execute("ALTER TABLE chat_session DROP CONSTRAINT IF EXISTS ck_chat_session_kind")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS kind")
