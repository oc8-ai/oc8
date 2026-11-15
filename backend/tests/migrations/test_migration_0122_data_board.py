"""0122: data board tables -- RLS, cascades, and an append-only record store
(no UPDATE grant on batches and records)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import oc8.models as m
from tests.conftest import AppSessionFactory

_AS_OF = dt.datetime(2026, 3, 31, tzinfo=dt.UTC)


async def _seed_board(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (agent_id, board_id, batch_id, record_id)."""
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Reporter")
        db.add(agent)
        await db.flush()
        board = m.DataBoard(
            tenant_id=tenant,
            department_id=dept.id,
            agent_id=agent.id,
            title="Revenue",
            spec={"datasets": {}},
        )
        db.add(board)
        await db.flush()
        batch = m.DataBoardBatch(
            tenant_id=tenant,
            board_id=board.id,
            dataset="revenue",
            as_of=_AS_OF,
            row_count=1,
        )
        db.add(batch)
        await db.flush()
        record = m.DataBoardRecord(
            tenant_id=tenant,
            board_id=board.id,
            batch_id=batch.id,
            dataset="revenue",
            as_of=_AS_OF,
            recorded_at=dt.datetime(2026, 4, 1, tzinfo=dt.UTC),
            values={"amount": 120},
        )
        db.add(record)
        await db.flush()
        return agent.id, board.id, batch.id, record.id


async def test_a_board_with_a_batch_and_records_round_trips(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    _agent, board_id, batch_id, record_id = await _seed_board(app_session, tenant)
    async with app_session(tenant) as db:
        board = await db.get(m.DataBoard, board_id)
        assert board is not None
        assert board.spec_version == 1
        assert board.description == ""
        assert board.removed_at is None
        assert board.deleted_at is None
        batch = await db.get(m.DataBoardBatch, batch_id)
        assert batch is not None
        assert batch.recorded_at is not None
        assert batch.run_id is None
        record = await db.get(m.DataBoardRecord, record_id)
        assert record is not None
        assert record.values == {"amount": 120}
        db.add(
            m.DataBoardSpecVersion(
                tenant_id=tenant, board_id=board_id, version=1, title="Revenue", spec={}
            )
        )
        await db.flush()


async def test_records_are_append_only(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed_board(app_session, tenant)
    with pytest.raises(DBAPIError) as exc:
        async with app_session(tenant) as db:
            await db.execute(text("UPDATE data_board_record SET values = '{}'"))
    assert "permission denied" in str(exc.value)


async def test_batches_are_append_only(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed_board(app_session, tenant)
    with pytest.raises(DBAPIError) as exc:
        async with app_session(tenant) as db:
            await db.execute(text("UPDATE data_board_batch SET row_count = 2"))
    assert "permission denied" in str(exc.value)


async def test_deleting_the_agent_keeps_the_board_with_agent_id_null(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, board_id, _batch, _record = await _seed_board(app_session, tenant)
    async with app_session(tenant) as db:
        await db.execute(text("DELETE FROM agent WHERE id = :i"), {"i": agent_id})
    async with app_session(tenant) as db:
        board = await db.get(m.DataBoard, board_id, populate_existing=True)
        assert board is not None
        assert board.agent_id is None


async def test_rls_hides_another_tenants_boards(app_session: AppSessionFactory) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _seed_board(app_session, tenant_a)
    async with app_session(tenant_b) as db:
        assert (await db.execute(select(m.DataBoard))).scalars().all() == []
        assert (await db.execute(select(m.DataBoardRecord))).scalars().all() == []
