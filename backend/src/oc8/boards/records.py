"""The one place data enters a board's append-only store.

A later batch for the same dataset and `as_of` replaces the whole period in views;
earlier batches stay, so "first reported" remains answerable.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import oc8.models as m
from oc8.boards.spec import (
    NUMERIC_TYPES,
    BoardSpecInvalid,
    DatasetSpec,
    normalize_as_of,
    parse_board_spec,
    validate_values,
)
from oc8.config import get_settings

MAX_BATCH_ROWS = 500


class BoardWriteRefused(ValueError):
    """A write was refused; `code` is stable, the message is meant for the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _refuse_repeated_total(ds: DatasetSpec, rows: Sequence[Mapping[str, object]]) -> None:
    """Refuse a batch that stamps its own total on every row.

    A KPI sums a field over the period's rows, so a total repeated on each row
    would be counted once per row.
    """
    if len(rows) < 2:
        return
    numbers: dict[str, list[float]] = {}
    for f in ds.fields:
        if f.type not in NUMERIC_TYPES:
            continue
        cells = [r.get(f.key) for r in rows]
        values = [float(v) for v in cells if isinstance(v, int | float)]
        if len(values) == len(cells):
            numbers[f.key] = values
    sums = {k: math.fsum(values) for k, values in numbers.items()}
    for f in ds.fields:
        # A percent KPI averages, so a repeated percent is harmless.
        repeated = numbers.get(f.key)
        if repeated is None or f.type == "percent" or repeated[0] == 0:
            continue
        if len(set(repeated)) != 1:
            continue
        for other, total in sums.items():
            if other != f.key and abs(total - repeated[0]) <= 0.005:
                raise BoardWriteRefused(
                    "repeated_total",
                    f"Field '{f.key}' repeats the batch total in every row; the dashboard "
                    f"adds rows up itself. Leave it out and point the KPI at '{other}'.",
                )


async def write_batch(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    board_id: uuid.UUID,
    dataset: str,
    as_of: str,
    rows: Sequence[Mapping[str, object]],
    note: str | None,
    run_id: uuid.UUID | None,
    now: dt.datetime | None = None,
) -> m.DataBoardBatch:
    # populate_existing: see a spec just changed or a board just removed.
    board = (
        await db.execute(
            select(m.DataBoard)
            .where(
                m.DataBoard.id == board_id,
                m.DataBoard.tenant_id == tenant_id,
                m.DataBoard.deleted_at.is_(None),
                m.DataBoard.removed_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if board is None:
        raise BoardWriteRefused("board_unknown", "No such board. It may have been removed.")
    if board.agent_id != agent.id:
        raise BoardWriteRefused("not_producer", "Only the board's producing agent may write to it.")

    try:
        spec = parse_board_spec(board.spec)
    except BoardSpecInvalid:
        raise BoardWriteRefused("board_unknown", "The board's spec is unusable.") from None
    ds = next((d for d in spec.datasets if d.key == dataset), None)
    if ds is None:
        valid = ", ".join(d.key for d in spec.datasets)
        raise BoardWriteRefused(
            "unknown_dataset", f"This board has no dataset '{dataset[:40]}'. Valid: {valid}."
        )
    try:
        period = normalize_as_of(as_of, ds.grain)
    except BoardSpecInvalid as exc:
        raise BoardWriteRefused("invalid_as_of", str(exc)) from None

    if not rows:
        raise BoardWriteRefused("empty", "Send at least one row.")
    if len(rows) > MAX_BATCH_ROWS:
        raise BoardWriteRefused(
            "too_many_rows", f"At most {MAX_BATCH_ROWS} rows per write; split the data."
        )
    clean: list[dict[str, object]] = []
    for i, row in enumerate(rows):
        try:
            clean.append(validate_values(ds, row))
        except BoardSpecInvalid as exc:
            raise BoardWriteRefused("invalid_values", f"Row {i + 1}: {exc}") from None
    _refuse_repeated_total(ds, clean)

    # The board lock serialises writers of one board; the advisory lock serialises
    # the cap check across all of a tenant's boards.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"board-cap:{tenant_id}"},
    )
    used = await db.scalar(
        select(func.count())
        .select_from(m.DataBoardRecord)
        .where(m.DataBoardRecord.tenant_id == tenant_id)
    )
    if int(used or 0) + len(clean) > get_settings().board_record_cap_per_tenant:
        raise BoardWriteRefused(
            "row_cap_reached",
            "The data store is full, so this write was not saved. "
            "Tell a person so they can free space.",
        )

    recorded_at = now or dt.datetime.now(dt.UTC)
    batch = m.DataBoardBatch(
        tenant_id=tenant_id,
        board_id=board.id,
        dataset=dataset,
        as_of=period,
        run_id=run_id,
        recorded_at=recorded_at,
        row_count=len(clean),
        note=note,
    )
    db.add(batch)
    await db.flush()
    db.add_all(
        m.DataBoardRecord(
            tenant_id=tenant_id,
            board_id=board.id,
            batch_id=batch.id,
            dataset=dataset,
            as_of=period,
            recorded_at=recorded_at,
            run_id=run_id,
            values=values,
        )
        for values in clean
    )
    await db.flush()
    return batch
