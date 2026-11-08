"""Connections: owners, trigger creators, and per-agent tool access.

* `oauth_connection.owner_member_id` / `credential.owner_member_id`: NULL is
  the organisation (every existing row stays that), otherwise one member's
  personal account. Uniqueness becomes per owner; NULLS NOT DISTINCT keeps
  today's rule for organisation rows exactly.
* `trigger.created_by_member_id`: whose account a schedule or webhook acts
  through when a tool runs "in the user's context". NULL for every existing
  trigger (re-saving one fills it).
* `agent_tool_access`: per agent and tool key, either "the acting person's own
  account" (`user`) or one fixed connection/credential (`fixed`). A fixed row
  whose target is deleted keeps mode `fixed` with no target, so the runtime
  refuses instead of quietly choosing something else.
* A foreign key does not see RLS, so a guard trigger refuses any reference to
  another tenant's member, agent, connection or credential.

Revision ID: 0107
Revises: 0106
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0107"
down_revision: str | None = "0106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD = """
CREATE OR REPLACE FUNCTION oc8_same_tenant_ref() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  ref_id uuid;
  ref_tenant uuid;
  i int := 0;
BEGIN
  -- TG_ARGV holds pairs: (column of NEW, table it references by id).
  WHILE i < TG_NARGS LOOP
    ref_id := (to_jsonb(NEW) ->> TG_ARGV[i])::uuid;
    IF ref_id IS NOT NULL THEN
      EXECUTE format('SELECT tenant_id FROM %I WHERE id = $1', TG_ARGV[i + 1])
        INTO ref_tenant USING ref_id;
      IF ref_tenant IS DISTINCT FROM NEW.tenant_id THEN
        RAISE EXCEPTION '%.% must reference a row of the same tenant', TG_TABLE_NAME, TG_ARGV[i]
          USING ERRCODE = 'foreign_key_violation';
      END IF;
    END IF;
    i := i + 2;
  END LOOP;
  RETURN NEW;
END $$
"""

_STATEMENTS = (
    _GUARD,
    # --- owners
    "ALTER TABLE oauth_connection ADD COLUMN IF NOT EXISTS owner_member_id uuid NULL "
    "REFERENCES org_member(id) ON DELETE CASCADE",
    "CREATE INDEX IF NOT EXISTS ix_oauth_connection_owner_member_id ON oauth_connection (owner_member_id)",
    "ALTER TABLE oauth_connection DROP CONSTRAINT IF EXISTS uq_oauth_conn_tenant_provider_account",
    "ALTER TABLE oauth_connection ADD CONSTRAINT uq_oauth_conn_tenant_provider_owner_account "
    "UNIQUE NULLS NOT DISTINCT (tenant_id, provider, owner_member_id, account_label)",
    "DROP TRIGGER IF EXISTS trg_oauth_connection_same_tenant ON oauth_connection",
    "CREATE TRIGGER trg_oauth_connection_same_tenant BEFORE INSERT OR UPDATE ON oauth_connection "
    "FOR EACH ROW EXECUTE FUNCTION oc8_same_tenant_ref('owner_member_id', 'org_member')",
    "ALTER TABLE credential ADD COLUMN IF NOT EXISTS owner_member_id uuid NULL "
    "REFERENCES org_member(id) ON DELETE CASCADE",
    "CREATE INDEX IF NOT EXISTS ix_credential_owner_member_id ON credential (owner_member_id)",
    "ALTER TABLE credential DROP CONSTRAINT IF EXISTS uq_credential_tenant_name",
    "ALTER TABLE credential ADD CONSTRAINT uq_credential_tenant_owner_name "
    "UNIQUE NULLS NOT DISTINCT (tenant_id, owner_member_id, name)",
    "DROP TRIGGER IF EXISTS trg_credential_same_tenant ON credential",
    "CREATE TRIGGER trg_credential_same_tenant BEFORE INSERT OR UPDATE ON credential "
    "FOR EACH ROW EXECUTE FUNCTION oc8_same_tenant_ref('owner_member_id', 'org_member')",
    # --- trigger creator
    "ALTER TABLE trigger ADD COLUMN IF NOT EXISTS created_by_member_id uuid NULL "
    "REFERENCES org_member(id) ON DELETE SET NULL",
    "DROP TRIGGER IF EXISTS trg_trigger_same_tenant ON trigger",
    "CREATE TRIGGER trg_trigger_same_tenant BEFORE INSERT OR UPDATE ON trigger "
    "FOR EACH ROW EXECUTE FUNCTION oc8_same_tenant_ref('created_by_member_id', 'org_member')",
    # --- per-agent tool access
    """
    CREATE TABLE IF NOT EXISTS agent_tool_access (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL,
        agent_id uuid NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
        tool_key text NOT NULL,
        mode text NOT NULL,
        oauth_connection_id uuid NULL REFERENCES oauth_connection(id) ON DELETE SET NULL,
        credential_id uuid NULL REFERENCES credential(id) ON DELETE SET NULL,
        set_by_member_id uuid NULL REFERENCES org_member(id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_agent_tool_access UNIQUE (agent_id, tool_key),
        CONSTRAINT ck_agent_tool_access_mode CHECK (mode IN ('user', 'fixed')),
        CONSTRAINT ck_agent_tool_access_target CHECK (
            (mode = 'user' AND oauth_connection_id IS NULL AND credential_id IS NULL)
            OR (mode = 'fixed' AND NOT (oauth_connection_id IS NOT NULL AND credential_id IS NOT NULL))
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_tool_access_tenant_id ON agent_tool_access (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_tool_access_agent_id ON agent_tool_access (agent_id)",
    "ALTER TABLE agent_tool_access ENABLE ROW LEVEL SECURITY",
    """
    DO $$ BEGIN
        CREATE POLICY tenant_isolation ON agent_tool_access
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oc8_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON agent_tool_access TO oc8_app;
      END IF;
    END $$
    """,
    "DROP TRIGGER IF EXISTS trg_agent_tool_access_same_tenant ON agent_tool_access",
    "CREATE TRIGGER trg_agent_tool_access_same_tenant BEFORE INSERT OR UPDATE ON agent_tool_access "
    "FOR EACH ROW EXECUTE FUNCTION oc8_same_tenant_ref('agent_id', 'agent', "
    "'oauth_connection_id', 'oauth_connection', 'credential_id', 'credential', "
    "'set_by_member_id', 'org_member')",
)


def add_connection_ownership(conn: sa.Connection) -> None:
    for statement in _STATEMENTS:
        conn.execute(sa.text(statement))


def remove_connection_ownership(conn: sa.Connection) -> None:
    for statement in (
        "DROP TABLE IF EXISTS agent_tool_access",
        "DROP TRIGGER IF EXISTS trg_trigger_same_tenant ON trigger",
        "ALTER TABLE trigger DROP COLUMN IF EXISTS created_by_member_id",
        "DROP TRIGGER IF EXISTS trg_credential_same_tenant ON credential",
        "ALTER TABLE credential DROP CONSTRAINT IF EXISTS uq_credential_tenant_owner_name",
        "ALTER TABLE credential DROP COLUMN IF EXISTS owner_member_id",
        "ALTER TABLE credential ADD CONSTRAINT uq_credential_tenant_name UNIQUE (tenant_id, name)",
        "DROP TRIGGER IF EXISTS trg_oauth_connection_same_tenant ON oauth_connection",
        "ALTER TABLE oauth_connection DROP CONSTRAINT IF EXISTS uq_oauth_conn_tenant_provider_owner_account",
        "ALTER TABLE oauth_connection DROP COLUMN IF EXISTS owner_member_id",
        "ALTER TABLE oauth_connection ADD CONSTRAINT uq_oauth_conn_tenant_provider_account "
        "UNIQUE (tenant_id, provider, account_label)",
        "DROP FUNCTION IF EXISTS oc8_same_tenant_ref()",
    ):
        conn.execute(sa.text(statement))


def upgrade() -> None:
    add_connection_ownership(op.get_bind())


def downgrade() -> None:
    remove_connection_ownership(op.get_bind())
