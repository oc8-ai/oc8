"""knowledge_base gains index_type / index_config / credential_id;

capa.type gains vector_index

External vector indexes (query-only) attach an existing Qdrant or pgvector
collection as a Knowledge Base without copying chunks into oc8. Secrets stay
on the unified Credential row; index_config holds only non-secret mapping.

Revision ID: 0098
Revises: 0097
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = (
    "'skill','flow_template','agent_template','department_template','connector',"
    "'model_adapter','runtime_adapter','tool_pack','approval_channel','core_extension'"
)
_NEW_TYPES = (
    "'skill','flow_template','agent_template','department_template','connector',"
    "'vector_index','model_adapter','runtime_adapter','tool_pack',"
    "'approval_channel','core_extension'"
)


def upgrade() -> None:
    # knowledge_base is in the create_all frozen set on fresh installs; IF NOT
    # EXISTS converges that path with incremental migration (same as 0069).
    op.execute(
        "ALTER TABLE knowledge_base "
        "ADD COLUMN IF NOT EXISTS index_type text NOT NULL DEFAULT 'internal'"
    )
    op.execute(
        "ALTER TABLE knowledge_base "
        "ADD COLUMN IF NOT EXISTS index_config jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute("ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS credential_id uuid")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_credential_id "
        "ON knowledge_base (credential_id)"
    )
    op.drop_constraint("ck_capa_type", "capa", type_="check")
    op.create_check_constraint("ck_capa_type", "capa", f"type IN ({_NEW_TYPES})")


def downgrade() -> None:
    op.drop_constraint("ck_capa_type", "capa", type_="check")
    op.create_check_constraint("ck_capa_type", "capa", f"type IN ({_OLD_TYPES})")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_base_credential_id")
    op.execute("ALTER TABLE knowledge_base DROP COLUMN IF EXISTS credential_id")
    op.execute("ALTER TABLE knowledge_base DROP COLUMN IF EXISTS index_config")
    op.execute("ALTER TABLE knowledge_base DROP COLUMN IF EXISTS index_type")
