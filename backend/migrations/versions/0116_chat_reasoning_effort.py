"""Chat reasoning effort: a chat's level, and whether a model can think.

`chat_session.effort` is the level a member picked for one chat (NULL is
"Default"). `model_config.supports_reasoning` gates whether any level or
effort is sent to that model at all, so existing rows are backfilled from the
same rule the model form prefills with (`oc8.modelrouter.reasoning_support`
via the provider declarations), including a capa provider loaded in this
process. A row whose operator already configured an `effort` counts as one
that can think: it was sent that value before this revision, and the new gate
must not silently drop it.

Revision ID: 0116
Revises: 0115
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0116"
down_revision: str | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _thinks(provider: str, model: str, params: Any) -> bool:
    effort = params.get("effort") if isinstance(params, dict) else None
    if isinstance(effort, str) and effort.strip():
        return True
    try:
        # Deferred: the registry imports the adapters (0031 does the same for
        # the settings it needs).
        from oc8.modelrouter.registry import supports_reasoning_for

        return supports_reasoning_for(provider or "", model or "")
    except Exception:
        # Never fail an upgrade over a guess: the admin can switch it on.
        return False


def upgrade() -> None:
    # Fail fast rather than queue the app behind a run's transaction (0112).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS effort text")
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE chat_session ADD CONSTRAINT ck_chat_session_effort
                CHECK (effort IS NULL OR effort IN ('fast','balanced','thorough'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute(
        "ALTER TABLE model_config "
        "ADD COLUMN IF NOT EXISTS supports_reasoning boolean NOT NULL DEFAULT false"
    )
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, provider, model, params FROM model_config")).fetchall()
    thinking = [{"id": row.id} for row in rows if _thinks(row.provider, row.model, row.params)]
    if thinking:
        conn.execute(
            sa.text("UPDATE model_config SET supports_reasoning = true WHERE id = :id"), thinking
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE chat_session DROP CONSTRAINT IF EXISTS ck_chat_session_effort")
    op.execute("ALTER TABLE chat_session DROP COLUMN IF EXISTS effort")
    op.execute("ALTER TABLE model_config DROP COLUMN IF EXISTS supports_reasoning")
