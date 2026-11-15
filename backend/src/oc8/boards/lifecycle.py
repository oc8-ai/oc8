"""Board lifecycle: archive and restore with the department, and delete a board.

Archiving a department archives its boards (`deleted_at`, the same `now`), and
restoring clears only those that were not deleted on their own (`removed_at IS
NULL`). Deleting a board removes its append-only history on purpose: the app role
keeps DELETE (not UPDATE) on those tables for exactly this.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select, update

from oc8 import models as m

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession


async def archive_department_boards(
    db: AsyncSession, *, department_id: uuid.UUID, now: dt.datetime
) -> int:
    """Archive the department's live boards; returns how many."""
    result = await db.execute(
        update(m.DataBoard)
        .where(m.DataBoard.department_id == department_id, m.DataBoard.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def restore_department_boards(db: AsyncSession, *, department_id: uuid.UUID) -> int:
    """Bring back the department's archived boards, except those a person removed."""
    result = await db.execute(
        update(m.DataBoard)
        .where(
            m.DataBoard.department_id == department_id,
            m.DataBoard.deleted_at.is_not(None),
            m.DataBoard.removed_at.is_(None),
        )
        .values(deleted_at=None)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def count_board_records(db: AsyncSession, board_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(m.DataBoardRecord)
                .where(m.DataBoardRecord.board_id == board_id)
            )
        ).scalar_one()
    )


async def remove_board(db: AsyncSession, board: m.DataBoard, *, now: dt.datetime) -> int:
    """Delete the board's records, batches and spec versions and mark it removed.

    The row stays (removed_at) so a stale link answers "removed". Returns the
    number of records deleted.
    """
    deleted = await db.execute(
        delete(m.DataBoardRecord).where(m.DataBoardRecord.board_id == board.id)
    )
    await db.execute(delete(m.DataBoardBatch).where(m.DataBoardBatch.board_id == board.id))
    await db.execute(
        delete(m.DataBoardSpecVersion).where(m.DataBoardSpecVersion.board_id == board.id)
    )
    board.removed_at = now
    await db.flush()
    return int(deleted.rowcount or 0)  # type: ignore[attr-defined]
