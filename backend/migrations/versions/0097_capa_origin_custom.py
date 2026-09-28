"""capa.origin gains a third value: custom

0009 enumerated the two origins a Capa row would accept -- `local` (disk-
discovered) and `store` (never actually used yet). The custom-MCP-capa
design (docs/superpowers/specs/2026-09-19-custom-mcp-capa-design.md, §1)
adds a third: `origin="custom"` marks a capa a tenant authored themselves
through the wizard, installed through the exact same `install_plugin`
pipeline as any catalog capa. The design's own §1 asserts this needs "no
schema change", which is wrong -- `ck_capa_origin` (renamed from
`ck_plugin_origin` by 0059) still rejects anything but `local`/`store`, so
`install_plugin(..., origin="custom")` 500s on `CheckViolationError` without
this widening.

Originally landed as revision 0094 alongside the api_key 0094; retargeted
onto 0096 so the chain has a single head.

Revision ID: 0097
Revises: 0096
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_ORIGINS = "'local','store'"
_NEW_ORIGINS = "'local','store','custom'"


def upgrade() -> None:
    op.drop_constraint("ck_capa_origin", "capa", type_="check")
    op.create_check_constraint("ck_capa_origin", "capa", f"origin IN ({_NEW_ORIGINS})")


def downgrade() -> None:
    op.drop_constraint("ck_capa_origin", "capa", type_="check")
    op.create_check_constraint("ck_capa_origin", "capa", f"origin IN ({_OLD_ORIGINS})")
