"""The generated mission section of an agent that produces dashboards.

Built at run start from the live spec, so a board edited since the agent was
configured is described as it is now, never from a stale copy.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import oc8.models as m
from oc8.boards.spec import BoardSpecInvalid, DatasetSpec, FieldSpec, parse_board_spec


def _field_line(f: FieldSpec) -> str:
    kind = f"{f.type} {f.currency}" if f.currency else f.type
    return f"      - {f.key} ({kind}): {f.label}"


def _dataset_lines(ds: DatasetSpec) -> list[str]:
    return [
        f"  Dataset `{ds.key}` -- {ds.label}. One period per {ds.grain}. Fields:",
        *(_field_line(f) for f in ds.fields),
    ]


async def board_section(db: AsyncSession, *, agent: m.Agent) -> str | None:
    """The agent's live boards and how to fill them, or None if it has none."""
    boards = (
        (
            await db.execute(
                select(m.DataBoard)
                .where(
                    m.DataBoard.tenant_id == agent.tenant_id,
                    m.DataBoard.agent_id == agent.id,
                    m.DataBoard.deleted_at.is_(None),
                    m.DataBoard.removed_at.is_(None),
                )
                .order_by(m.DataBoard.created_at, m.DataBoard.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    blocks: list[str] = []
    for board in boards:
        try:
            spec = parse_board_spec(board.spec)
        except BoardSpecInvalid:
            continue
        lines = [f"- Board {board.id}: {board.title}"]
        for ds in spec.datasets:
            lines.extend(_dataset_lines(ds))
        blocks.append("\n".join(lines))
    if not blocks:
        return None
    return (
        "You fill these dashboards with data. People read them as charts and tables:\n"
        + "\n".join(blocks)
        + "\n\nCall `board_write` with `board` (the id above), `dataset` (its key) and `as_of` "
        "(the period, matching the dataset's grain), plus either `values` (one object of "
        "field values) or `rows` (a list of such objects, at most 500). Send only the fields "
        "listed, with the declared types: numbers as numbers, never as text. Always report "
        "the complete period -- the latest write for a period replaces earlier ones on the "
        "dashboard, so a partial write hides the rest. Add a short `note` (at most 2000 "
        "characters) when the figures need context."
    )
