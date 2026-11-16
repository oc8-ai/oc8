"""write_batch: validation, producer check, and the per-tenant row cap."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import oc8.models as m
from oc8.boards.records import MAX_BATCH_ROWS, BoardWriteRefused, write_batch
from oc8.config import get_settings
from tests.conftest import AppSessionFactory

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


async def _seed(db: AsyncSession, tenant: uuid.UUID) -> tuple[m.Agent, m.DataBoard]:
    dept = m.Department(tenant_id=tenant, name="Finance", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Reporter")
    db.add(agent)
    await db.flush()
    board = m.DataBoard(
        tenant_id=tenant, department_id=dept.id, agent_id=agent.id, title="Revenue", spec=_SPEC
    )
    db.add(board)
    await db.flush()
    return agent, board


async def _write(
    db: AsyncSession,
    tenant: uuid.UUID,
    agent: m.Agent,
    board: m.DataBoard,
    rows: list[dict[str, object]],
    **kw: Any,
) -> m.DataBoardBatch:
    args: dict[str, Any] = {
        "dataset": "sales",
        "as_of": "2026-03-01",
        "note": None,
        "run_id": None,
    }
    args.update(kw)
    return await write_batch(
        db, tenant_id=tenant, agent=agent, board_id=board.id, rows=rows, **args
    )


async def _counts(db: AsyncSession) -> tuple[int, int]:
    recs = await db.scalar(select(func.count()).select_from(m.DataBoardRecord))
    batches = await db.scalar(select(func.count()).select_from(m.DataBoardBatch))
    return int(recs or 0), int(batches or 0)


async def test_a_batch_stores_every_row_with_one_recorded_at(
    app_session: AppSessionFactory,
) -> None:
    tenant, run = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        batch = await _write(
            db,
            tenant,
            agent,
            board,
            [{"region": "north", "amount": 100}, {"region": "south", "amount": 50}],
            note="Two regions",
            run_id=run,
        )
        assert batch.row_count == 2 and batch.note == "Two regions"
        records = (await db.scalars(select(m.DataBoardRecord))).all()
        assert len(records) == 2
        for r in records:
            assert r.batch_id == batch.id
            assert r.recorded_at == batch.recorded_at
            assert r.run_id == run
            assert r.as_of == dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
            assert r.dataset == "sales"
        assert {r.values["region"] for r in records} == {"north", "south"}


async def test_only_the_producing_agent_may_write(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        other = m.Agent(tenant_id=tenant, department_id=agent.department_id, name="Other")
        db.add(other)
        await db.flush()
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, other, board, [{"amount": 1}])
        assert exc.value.code == "not_producer"


async def test_a_removed_or_archived_board_refuses(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        board.removed_at = dt.datetime.now(dt.UTC)
        await db.flush()
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, agent, board, [{"amount": 1}])
        assert exc.value.code == "board_unknown"
        board.removed_at = None
        board.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        with pytest.raises(BoardWriteRefused) as exc2:
            await _write(db, tenant, agent, board, [{"amount": 1}])
        assert exc2.value.code == "board_unknown"
        with pytest.raises(BoardWriteRefused) as exc3:
            await write_batch(
                db,
                tenant_id=tenant,
                agent=agent,
                board_id=uuid.uuid4(),
                dataset="sales",
                as_of="2026-03-01",
                rows=[{"amount": 1}],
                note=None,
                run_id=None,
            )
        assert exc3.value.code == "board_unknown"


async def test_unknown_dataset_names_the_valid_ones(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, agent, board, [{"amount": 1}], dataset="nope")
        assert exc.value.code == "unknown_dataset"
        assert "sales" in str(exc.value)


async def test_refusal_writes_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, agent, board, [{"amount": 1}, {"amount": "12"}])
        assert exc.value.code == "invalid_values"
        assert await _counts(db) == (0, 0)


async def test_bad_as_of_and_empty_are_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, agent, board, [{"amount": 1}], as_of="soon")
        assert exc.value.code == "invalid_as_of"
        with pytest.raises(BoardWriteRefused) as exc2:
            await _write(db, tenant, agent, board, [])
        assert exc2.value.code == "empty"
        assert await _counts(db) == (0, 0)


async def test_more_than_500_rows_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        rows: list[dict[str, object]] = [{"amount": 1}] * (MAX_BATCH_ROWS + 1)
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, agent, board, rows)
        assert exc.value.code == "too_many_rows"
        ok = await _write(db, tenant, agent, board, rows[:MAX_BATCH_ROWS])
        assert ok.row_count == MAX_BATCH_ROWS


async def test_row_cap_is_per_tenant(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "board_record_cap_per_tenant", 3)
    tenant, other_tenant = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        await _write(db, tenant, agent, board, [{"amount": 1}, {"amount": 2}])
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, agent, board, [{"amount": 3}, {"amount": 4}])
        assert exc.value.code == "row_cap_reached"
        assert await _counts(db) == (2, 1)
    async with app_session(other_tenant) as db:
        agent2, board2 = await _seed(db, other_tenant)
        batch = await _write(db, other_tenant, agent2, board2, [{"amount": 1}, {"amount": 2}])
        assert batch.row_count == 2


_TOTALS_SPEC: dict[str, Any] = {
    "datasets": [
        {
            "key": "sales",
            "label": "Sales",
            "grain": "day",
            "fields": [
                {"key": "amount", "label": "Amount", "type": "money", "currency": "EUR"},
                {"key": "total_amount", "label": "Total", "type": "money", "currency": "EUR"},
                {"key": "count", "label": "Count", "type": "number"},
                {"key": "customer", "label": "Customer", "type": "category"},
            ],
        }
    ],
    "blocks": [{"type": "kpi", "dataset": "sales", "field": "total_amount"}],
}


async def test_a_batch_total_repeated_on_every_row_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        board.spec = _TOTALS_SPEC
        await db.flush()
        rows: list[dict[str, object]] = [
            {"amount": 100.5, "total_amount": 350.75, "customer": "A"},
            {"amount": 200.25, "total_amount": 350.75, "customer": "B"},
            {"amount": 50.0, "total_amount": 350.75, "customer": "C"},
        ]
        with pytest.raises(BoardWriteRefused) as exc:
            await _write(db, tenant, agent, board, rows)
        assert exc.value.code == "repeated_total"
        message = str(exc.value)
        assert "'total_amount'" in message and "'amount'" in message
        assert "350" not in message
        assert await _counts(db) == (0, 0)


async def test_equal_per_row_values_that_are_not_a_total_are_accepted(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, board = await _seed(db, tenant)
        board.spec = _TOTALS_SPEC
        await db.flush()
        rows: list[dict[str, object]] = [{"amount": 269.1, "count": 1}] * 3
        batch = await _write(db, tenant, agent, board, rows)
        assert batch.row_count == 3
