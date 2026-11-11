"""oc8 AI model configs can think.

0116 backfilled `model_config.supports_reasoning` from the provider
declarations, but the oc8 AI provider (`opaas_ai`, aliases `opaas` and
`odoo-gpt`) only declares reasoning support from capa version 1.0.2 on, so
every existing oc8 AI row stayed at false and the chat's effort control never
showed for it. Switch them on; the provider now prefills new rows the same way.
Downgrade leaves the rows as they are (an admin may have set them since).

Revision ID: 0117
Revises: 0116
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0117"
down_revision: str | None = "0116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE model_config SET supports_reasoning = true "
        "WHERE lower(provider) IN ('opaas_ai', 'opaas', 'odoo-gpt') AND NOT supports_reasoning"
    )


def downgrade() -> None:
    pass
