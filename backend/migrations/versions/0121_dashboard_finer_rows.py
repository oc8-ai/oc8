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


def _rescale(y_expr: str, h_expr: str, guard: str) -> None:
    """Rescale only elements whose y and h are whole numbers inside the bounds
    in `guard`; anything else (null, text, fractional, out of range) is left
    unchanged, so one odd stored value can never abort the migration."""
    for table in _TABLES:
        op.execute(f"""
            UPDATE {table} SET widgets = (
              SELECT coalesce(jsonb_agg(
                       CASE WHEN jsonb_typeof(w->'y') = 'number'
                                 AND jsonb_typeof(w->'h') = 'number'
                                 AND (w->>'y')::numeric = trunc((w->>'y')::numeric)
                                 AND (w->>'h')::numeric = trunc((w->>'h')::numeric)
                                 AND {guard}
                            THEN w || jsonb_build_object('y', {y_expr}, 'h', {h_expr})
                            ELSE w END
                       ORDER BY ord), '[]'::jsonb)
              FROM jsonb_array_elements(widgets) WITH ORDINALITY AS t(w, ord))
            WHERE jsonb_typeof(widgets) = 'array'
        """)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Results stay within the API bounds (y <= 10000, h <= 1000).
    _rescale(
        "(w->>'y')::numeric * 2",
        "(w->>'h')::numeric * 2",
        "(w->>'y')::numeric BETWEEN 0 AND 5000 AND (w->>'h')::numeric BETWEEN 1 AND 500",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _rescale(
        "((w->>'y')::numeric / 2)::int",
        "greatest(1, ((w->>'h')::numeric / 2)::int)",
        "(w->>'y')::numeric BETWEEN 0 AND 10000 AND (w->>'h')::numeric BETWEEN 1 AND 1000",
    )
