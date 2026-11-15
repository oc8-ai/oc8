"""kb_chunk may die because an admin narrowed its source's selection

`ck_kb_chunk_deleted_reason` (0045) names every legal `deleted_reason`, so the
prune a scope-narrowing edit triggers (`oc8.knowledge.scope_prune`) needs its
own value there. Widening a CHECK only admits rows; nothing existing changes.

Revision ID: 0123
Revises: 0122
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0123"
down_revision: str | None = "0122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BEFORE = "'operator_delete','source_absent','superseded'"
_AFTER = "'operator_delete','source_absent','superseded','scope_changed'"


def _constraint(reasons: str, *, validate: bool) -> None:
    op.execute("ALTER TABLE kb_chunk DROP CONSTRAINT IF EXISTS ck_kb_chunk_deleted_reason")
    op.execute(
        "ALTER TABLE kb_chunk ADD CONSTRAINT ck_kb_chunk_deleted_reason CHECK ("
        "(deleted_at IS NULL AND deleted_reason IS NULL) OR "
        f"(deleted_at IS NOT NULL AND deleted_reason = ANY (ARRAY[{reasons}])))"
        + ("" if validate else " NOT VALID")
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _constraint(_AFTER, validate=True)


def downgrade() -> None:
    """Narrows the CHECK again as NOT VALID: new rows are held to the old set,
    while a row already tombstoned as `scope_changed` keeps its reason.
    Rewriting it would misstate why the document left, and validating would
    make the downgrade fail on any database that ever pruned a scope."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    _constraint(_BEFORE, validate=False)
