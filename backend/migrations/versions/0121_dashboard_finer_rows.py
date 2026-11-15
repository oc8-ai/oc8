"""Dashboard grid rows halve in height (80 px -> 40 px).

Stored widget positions are in row units, so every saved layout and preset
doubles its `y` and `h` to keep looking the same. Downgrade halves them again
(`h` never below one row). Element order is kept.

Revision ID: 0121
Revises: 0120
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0121"
down_revision: str | None = "0120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("member_dashboard_layout", "dashboard_preset")


def _rescale(y_expr: str, h_expr: str) -> None:
    for table in _TABLES:
        op.execute(f"""
            UPDATE {table} SET widgets = (
              SELECT coalesce(jsonb_agg(
                       CASE WHEN jsonb_typeof(w->'y') = 'number'
                                 AND jsonb_typeof(w->'h') = 'number'
                            THEN w || jsonb_build_object('y', {y_expr}, 'h', {h_expr})
                            ELSE w END
                       ORDER BY ord), '[]'::jsonb)
              FROM jsonb_array_elements(widgets) WITH ORDINALITY AS t(w, ord))
            WHERE jsonb_typeof(widgets) = 'array'
        """)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _rescale("(w->>'y')::int * 2", "(w->>'h')::int * 2")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _rescale("(w->>'y')::int / 2", "greatest(1, (w->>'h')::int / 2)")
