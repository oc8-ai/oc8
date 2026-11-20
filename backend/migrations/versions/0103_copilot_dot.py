"""Copilot as dot (design 2026-09-27-ai-workplace-collaboration-design.md §7a):
copilot_profile, responsibility, follow-up columns on trigger, kind='once',
chat_message.role='followup'.

IF NOT EXISTS / DROP ... IF EXISTS throughout, for idempotency: trigger and
chat_message are not in 0001's frozen set (0012 and 0077 create them with
explicit columns), but a re-run or a partially applied upgrade must not fail.

Revision ID: 0103
Revises: 0102
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0103"
down_revision: str | None = "0102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        DO $$ BEGIN
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS copilot_profile (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL,
            display_name text NOT NULL DEFAULT 'Copilot',
            avatar jsonb NOT NULL DEFAULT '{"shape": "round", "color": "indigo"}'::jsonb,
            paused_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_copilot_profile_member UNIQUE (tenant_id, member_id),
            CONSTRAINT ck_copilot_profile_name CHECK (char_length(display_name) BETWEEN 1 AND 40)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_copilot_profile_tenant_id ON copilot_profile (tenant_id)"
    )
    _rls("copilot_profile")

    op.execute("""
        CREATE TABLE IF NOT EXISTS responsibility (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            member_id uuid NOT NULL,
            chat_session_id uuid NOT NULL,
            title text NOT NULL,
            goal text NOT NULL,
            state text NOT NULL DEFAULT 'active',
            next_step text NOT NULL DEFAULT '',
            notify_rule text NOT NULL DEFAULT 'risks_and_decisions',
            origin_channel text,
            last_update_at timestamptz,
            last_report_run_id uuid,
            created_by_run_id uuid,
            closed_at timestamptz,
            close_reason text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_responsibility_state
                CHECK (state IN ('active','waiting','paused','done','cancelled')),
            CONSTRAINT ck_responsibility_notify_rule
                CHECK (notify_rule IN ('decisions_only','risks_and_decisions','every_update')),
            CONSTRAINT ck_responsibility_goal CHECK (char_length(goal) > 0)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_responsibility_tenant_id ON responsibility (tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_responsibility_member_state "
        "ON responsibility (tenant_id, member_id, state)"
    )
    _rls("responsibility")

    for col, typ in (
        ("chat_session_id", "uuid"),
        ("responsibility_id", "uuid"),
        ("timezone", "text"),
        ("ends_at", "timestamptz"),
        ("last_skip_reason", "text"),
    ):
        op.execute(f"ALTER TABLE trigger ADD COLUMN IF NOT EXISTS {col} {typ}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_trigger_chat_session_id ON trigger (chat_session_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trigger_responsibility_id ON trigger (responsibility_id)"
    )
    op.execute("ALTER TABLE trigger DROP CONSTRAINT IF EXISTS ck_trigger_kind")
    op.execute(
        "ALTER TABLE trigger ADD CONSTRAINT ck_trigger_kind "
        "CHECK (kind IN ('cron','event','webhook','once'))"
    )
    op.execute("ALTER TABLE trigger DROP CONSTRAINT IF EXISTS ck_trigger_kind_fields")
    op.execute("""
        ALTER TABLE trigger ADD CONSTRAINT ck_trigger_kind_fields CHECK (
            (kind = 'cron' AND cron_expression IS NOT NULL
             AND event_source IS NULL AND event_type IS NULL AND webhook_token IS NULL)
            OR (kind = 'event' AND event_source IS NOT NULL AND event_type IS NOT NULL
             AND cron_expression IS NULL AND webhook_token IS NULL)
            OR (kind = 'webhook' AND webhook_token IS NOT NULL
             AND cron_expression IS NULL AND event_source IS NULL AND event_type IS NULL)
            OR (kind = 'once' AND next_run_at IS NOT NULL AND cron_expression IS NULL
             AND event_source IS NULL AND event_type IS NULL AND webhook_token IS NULL)
        )
    """)
    op.execute("ALTER TABLE trigger DROP CONSTRAINT IF EXISTS ck_trigger_followup_fields")
    op.execute("""
        ALTER TABLE trigger ADD CONSTRAINT ck_trigger_followup_fields CHECK (
            chat_session_id IS NULL OR (responsibility_id IS NOT NULL AND timezone IS NOT NULL
            AND (kind <> 'cron' OR ends_at IS NOT NULL) AND kind IN ('cron','once'))
        )
    """)

    op.execute("ALTER TABLE chat_message DROP CONSTRAINT IF EXISTS ck_chat_message_role")
    op.execute(
        "ALTER TABLE chat_message ADD CONSTRAINT ck_chat_message_role "
        "CHECK (role IN ('user','assistant','followup'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM chat_message WHERE role = 'followup'")
    op.execute("ALTER TABLE chat_message DROP CONSTRAINT IF EXISTS ck_chat_message_role")
    op.execute(
        "ALTER TABLE chat_message ADD CONSTRAINT ck_chat_message_role "
        "CHECK (role IN ('user','assistant'))"
    )
    op.execute("DELETE FROM trigger WHERE kind = 'once' OR chat_session_id IS NOT NULL")
    op.execute("ALTER TABLE trigger DROP CONSTRAINT IF EXISTS ck_trigger_followup_fields")
    op.execute("ALTER TABLE trigger DROP CONSTRAINT IF EXISTS ck_trigger_kind_fields")
    op.execute("""
        ALTER TABLE trigger ADD CONSTRAINT ck_trigger_kind_fields CHECK (
            (kind = 'cron' AND cron_expression IS NOT NULL
             AND event_source IS NULL AND event_type IS NULL AND webhook_token IS NULL)
            OR (kind = 'event' AND event_source IS NOT NULL AND event_type IS NOT NULL
             AND cron_expression IS NULL AND webhook_token IS NULL)
            OR (kind = 'webhook' AND webhook_token IS NOT NULL
             AND cron_expression IS NULL AND event_source IS NULL AND event_type IS NULL)
        )
    """)
    op.execute("ALTER TABLE trigger DROP CONSTRAINT IF EXISTS ck_trigger_kind")
    op.execute(
        "ALTER TABLE trigger ADD CONSTRAINT ck_trigger_kind "
        "CHECK (kind IN ('cron','event','webhook'))"
    )
    for col in ("last_skip_reason", "ends_at", "timezone", "responsibility_id", "chat_session_id"):
        op.execute(f"ALTER TABLE trigger DROP COLUMN IF EXISTS {col}")
    op.execute("DROP TABLE IF EXISTS responsibility")
    op.execute("DROP TABLE IF EXISTS copilot_profile")
