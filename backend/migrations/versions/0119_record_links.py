"""Record links: the tenant's issued-link registry and two approval columns.

`record_link` holds every URL oc8 put into a tool result (`oc8_url`), so a
final answer can be checked: a link to a connected system that oc8 never
issued is a guess and is unlinked before anyone sees it (design doc
2026-11-13-record-links-design.md §4.2/§5). Scoped to the conversation the
link was issued in (`scope`: `chat:<session>`, else `task:<root task>`), not
the tenant: a tenant-wide registry would tell any member which records any
agent touched. Delegated runs and wake runs inherit the chat session, so the
copilot can still pass on a delegated agent's links.

`canon` is where a link leads however it is spelled (`records.registry.canonical`),
so the web UI's check of a browser-re-encoded href is one index lookup.

`approval_request.related_links` lists the existing records a held CREATE
references; `result_url` is the record the approved call produced.

Revision ID: 0119
Revises: 0118
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0119"
down_revision: str | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS record_link (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            scope text NOT NULL,
            url text NOT NULL,
            host text NOT NULL,
            canon text,
            issued_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_record_link_tenant_scope_url UNIQUE (tenant_id, scope, url),
            CONSTRAINT ck_record_link_url CHECK (char_length(url) <= 2000)
        )
    """)
    # A database that ran an earlier draft of this revision lacks `canon` and
    # `scope`. Its unscoped rows belong to no conversation: dropped, they only
    # cost links that are reissued on the next call.
    op.execute("ALTER TABLE record_link ADD COLUMN IF NOT EXISTS canon text")
    op.execute("ALTER TABLE record_link ADD COLUMN IF NOT EXISTS scope text")
    op.execute("DELETE FROM record_link WHERE scope IS NULL")
    op.execute("ALTER TABLE record_link ALTER COLUMN scope SET NOT NULL")
    op.execute("ALTER TABLE record_link DROP CONSTRAINT IF EXISTS uq_record_link_tenant_url")
    op.execute("DROP INDEX IF EXISTS ix_record_link_tenant_canon")
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE record_link ADD CONSTRAINT uq_record_link_tenant_scope_url
                UNIQUE (tenant_id, scope, url);
        EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
        END $$
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_record_link_tenant_host ON record_link (tenant_id, host)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_record_link_tenant_scope_canon "
        "ON record_link (tenant_id, scope, canon)"
    )
    op.execute("ALTER TABLE record_link ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON record_link
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON record_link TO oc8_app;
          END IF;
        END $$
    """)
    op.execute(
        "ALTER TABLE approval_request ADD COLUMN IF NOT EXISTS related_links jsonb "
        "NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE approval_request ADD COLUMN IF NOT EXISTS result_url text")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE approval_request DROP COLUMN IF EXISTS result_url")
    op.execute("ALTER TABLE approval_request DROP COLUMN IF EXISTS related_links")
    op.execute("DROP TABLE IF EXISTS record_link")
