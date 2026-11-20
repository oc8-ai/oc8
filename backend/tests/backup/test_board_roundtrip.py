"""A data board survives a backup and restore with its append-only history intact."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from oc8 import models as m
from oc8.backup.archive import export_archive
from oc8.backup.service import restore_from_archive
from oc8.boards.query import BoardQuery, Period, QueryResult, run_query
from oc8.boards.records import write_batch
from tests.backup.test_restore_service import (  # noqa: F401
    _kek,
    acme_tenant,
    actor,
    other_instance,
    other_tenant,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SPEC: dict[str, Any] = {
    "datasets": [
        {
            "key": "sales",
            "label": "Sales",
            "grain": "day",
            "fields": [
                {"key": "amount", "label": "Amount", "type": "money", "currency": "EUR"},
                {"key": "region", "label": "Region", "type": "category"},
            ],
        }
    ],
    "blocks": [{"type": "kpi", "dataset": "sales", "field": "amount"}],
}


def _query(reported: str) -> BoardQuery:
    return BoardQuery(
        dataset="sales",
        fields=["amount"],
        period=Period(from_=dt.date(2026, 3, 1), to=dt.date(2026, 4, 1)),
        reported=reported,  # type: ignore[arg-type]
    )


def _totals(result: QueryResult) -> list[tuple[str | None, dict[str, float | None]]]:
    return [(t.group, t.values) for t in result.totals]


async def _counts(db: Any, tenant: uuid.UUID) -> tuple[int, int, int]:
    out = []
    for model in (m.DataBoard, m.DataBoardBatch, m.DataBoardRecord):
        out.append(
            (
                await db.execute(
                    select(func.count()).select_from(model).where(model.tenant_id == tenant)
                )
            ).scalar_one()
        )
    return out[0], out[1], out[2]


async def test_board_history_survives_a_restore_into_another_instance(
    app_session: AppSessionFactory,
    other_instance: AppSessionFactory,  # noqa: F811
    acme_tenant: uuid.UUID,  # noqa: F811
    other_tenant: uuid.UUID,  # noqa: F811
    actor: uuid.UUID,  # noqa: F811
) -> None:
    t0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC)
    async with app_session(acme_tenant) as db:
        dept = (
            (await db.execute(select(m.Department).where(m.Department.tenant_id == acme_tenant)))
            .scalars()
            .first()
        )
        agent = (
            (await db.execute(select(m.Agent).where(m.Agent.tenant_id == acme_tenant)))
            .scalars()
            .first()
        )
        assert dept is not None and agent is not None
        board = m.DataBoard(
            tenant_id=acme_tenant,
            department_id=dept.id,
            agent_id=agent.id,
            title="Revenue",
            spec=_SPEC,
        )
        db.add(board)
        await db.flush()
        for n, rows in enumerate(
            [
                [{"region": "north", "amount": 100}, {"region": "south", "amount": 50}],
                [{"region": "north", "amount": 120}],
            ]
        ):
            await write_batch(
                db,
                tenant_id=acme_tenant,
                agent=agent,
                board_id=board.id,
                dataset="sales",
                as_of="2026-03-01",
                rows=rows,
                note=None,
                run_id=None,
                now=t0 + dt.timedelta(minutes=n),
            )
        board_id = board.id
        original_counts = await _counts(db, acme_tenant)
        original_latest = await run_query(db, board=board, query=_query("latest"))
        original_first = await run_query(db, board=board, query=_query("first"))
        _, backup = await export_archive(db, tenant_id=acme_tenant, passphrase=None)

    assert original_counts == (1, 2, 3)
    assert _totals(original_latest) != _totals(original_first)

    async with other_instance(other_tenant) as target:
        await restore_from_archive(
            target,
            tenant_id=other_tenant,
            archive_bytes=backup,
            passphrase=None,
            actor_type="human",
            actor_id=actor,
        )
        restored = (
            await target.execute(select(m.DataBoard).where(m.DataBoard.id == board_id))
        ).scalar_one()
        assert restored.tenant_id == other_tenant
        assert await _counts(target, other_tenant) == original_counts
        restored_latest = await run_query(target, board=restored, query=_query("latest"))
        restored_first = await run_query(target, board=restored, query=_query("first"))

    assert _totals(restored_latest) == _totals(original_latest)
    assert _totals(restored_first) == _totals(original_first)
