"""Board lifecycle: department cascades, deleting a board, and the grants and
constraints of the append-only tables (migration 0122)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import oc8.models as m
from oc8.agents.retire import delete_or_archive_agent
from oc8.auth import Principal, get_identity_provider
from oc8.authz.scope import scope_for_principal
from oc8.boards.lifecycle import (
    archive_department_boards,
    lock_live_board,
    remove_board,
    restore_department_boards,
)
from oc8.boards.records import write_batch
from oc8.boards.repo import visible_boards
from oc8.copilot.proposals import apply_proposal, create_proposal
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SPEC: dict[str, Any] = {
    "datasets": [
        {
            "key": "sales",
            "label": "Sales",
            "grain": "day",
            "fields": [{"key": "amount", "label": "Amount", "type": "money", "currency": "EUR"}],
        }
    ],
    "blocks": [{"type": "kpi", "dataset": "sales", "field": "amount"}],
}
_NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC)


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="boss", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


class _Seed:
    tenant: uuid.UUID
    dept: uuid.UUID
    agent: uuid.UUID
    board: uuid.UUID


async def _seed(
    app_session: AppSessionFactory,
    *,
    with_agent: bool = True,
    tenant: uuid.UUID | None = None,
    name: str = "Finance",
) -> _Seed:
    s = _Seed()
    s.tenant = tenant or uuid.uuid4()
    async with app_session(s.tenant) as db:
        dept = m.Department(tenant_id=s.tenant, name=name, frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=s.tenant, department_id=dept.id, name="Reporter", definition={})
        db.add(agent)
        await db.flush()
        board = m.DataBoard(
            tenant_id=s.tenant,
            department_id=dept.id,
            agent_id=agent.id,
            title="Revenue",
            spec=_SPEC,
        )
        db.add(board)
        await db.flush()
        db.add(
            m.DataBoardSpecVersion(
                tenant_id=s.tenant, board_id=board.id, version=1, title="Revenue", spec=_SPEC
            )
        )
        await write_batch(
            db,
            tenant_id=s.tenant,
            agent=agent,
            board_id=board.id,
            dataset="sales",
            as_of="2026-03-01",
            rows=[{"amount": 1}, {"amount": 2}],
            note=None,
            run_id=None,
            now=_NOW,
        )
        s.dept, s.agent, s.board = dept.id, agent.id, board.id
        if not with_agent:
            await db.delete(agent)
            await db.flush()
    return s


async def _listed(app_session: AppSessionFactory, s: _Seed) -> list[uuid.UUID]:
    async with app_session(s.tenant) as db:
        principal = Principal(subject="boss", tenant_id=s.tenant, role="org_admin")
        _member, scope = await scope_for_principal(db, principal, upsert=True)
        boards = await visible_boards(db, scope=scope, tenant_wide=True, tenant_id=s.tenant)
        return [b.id for b in boards]


async def _board(app_session: AppSessionFactory, s: _Seed) -> m.DataBoard | None:
    async with app_session(s.tenant) as db:
        return (
            await db.execute(select(m.DataBoard).where(m.DataBoard.id == s.board))
        ).scalar_one_or_none()


async def test_archiving_a_department_archives_its_boards(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as c:
        r = await c.delete(f"/api/v1/departments/{s.dept}", headers=_headers(s.tenant))
    assert r.status_code == 200 and r.json() == {"outcome": "archived"}, r.text
    board = await _board(app_session, s)
    assert board is not None and board.deleted_at is not None
    assert await _listed(app_session, s) == []


async def test_restoring_the_department_restores_its_boards(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with _http() as c:
        h = _headers(s.tenant)
        await c.delete(f"/api/v1/departments/{s.dept}", headers=h)
        r = await c.post(f"/api/v1/departments/{s.dept}/restore", headers=h)
        assert r.status_code == 200, r.text
        got = await c.get("/api/v1/boards", headers=h)
    assert [b["id"] for b in got.json()] == [str(s.board)]
    # The agent was archived with the department and stays archived.
    assert got.json()[0]["stale"] is True
    board = await _board(app_session, s)
    assert board is not None and board.deleted_at is None


async def test_restore_leaves_a_removed_board_removed(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        board = (await db.execute(select(m.DataBoard))).scalar_one()
        await remove_board(db, board, now=_NOW)
        assert await archive_department_boards(db, department_id=s.dept, now=_NOW) == 1
        assert await restore_department_boards(db, department_id=s.dept) == 0
        await db.refresh(board)
        assert board.deleted_at == _NOW and board.removed_at == _NOW


async def test_copilot_department_delete_and_restore_cascade_too(
    app_session: AppSessionFactory, acme_tenant: uuid.UUID
) -> None:
    s = await _seed(app_session, tenant=acme_tenant, name="Lifecycle copilot dept")
    actor = Principal(subject="operator-1", tenant_id=acme_tenant, role="org_admin")
    async with app_session(acme_tenant) as db:
        proposal = await create_proposal(
            db, actor, [{"type": "department.delete", "departmentId": str(s.dept)}]
        )
        assert (await apply_proposal(db, proposal.id, actor)).status == "applied"
    board = await _board(app_session, s)
    assert board is not None and board.deleted_at is not None
    async with app_session(acme_tenant) as db:
        proposal = await create_proposal(
            db, actor, [{"type": "department.restore", "departmentId": str(s.dept)}]
        )
        assert (await apply_proposal(db, proposal.id, actor)).status == "applied"
    board = await _board(app_session, s)
    assert board is not None and board.deleted_at is None


async def test_hard_deleted_department_takes_its_boards(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session, with_agent=False)
    async with _http() as c:
        r = await c.delete(f"/api/v1/departments/{s.dept}", headers=_headers(s.tenant))
    assert r.json() == {"outcome": "deleted"}, r.text
    assert await _board(app_session, s) is None
    async with app_session(s.tenant) as db:
        n = (await db.execute(select(func.count()).select_from(m.DataBoardRecord))).scalar_one()
        assert n == 0


async def test_hard_deleted_agent_leaves_a_stale_board_with_history(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        agent = (await db.execute(select(m.Agent).where(m.Agent.id == s.agent))).scalar_one()
        principal = Principal(subject="boss", tenant_id=s.tenant, role="org_admin")
        outcome = await delete_or_archive_agent(db, agent, principal=principal)
        assert outcome == "deleted"
    board = await _board(app_session, s)
    assert board is not None and board.agent_id is None
    async with app_session(s.tenant) as db:
        n = (await db.execute(select(func.count()).select_from(m.DataBoardRecord))).scalar_one()
        assert n == 2


async def test_remove_board_deletes_history_as_the_app_role(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        who = (await db.execute(text("select current_user"))).scalar_one()
        assert who == "oc8_app"
        board = (await db.execute(select(m.DataBoard))).scalar_one()
        assert await remove_board(db, board, now=_NOW) == 2
    async with app_session(s.tenant) as db:
        for model in (m.DataBoardRecord, m.DataBoardBatch, m.DataBoardSpecVersion):
            n = (await db.execute(select(func.count()).select_from(model))).scalar_one()
            assert n == 0, model.__name__
        board = (await db.execute(select(m.DataBoard))).scalar_one()
        assert board.removed_at == _NOW


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("data_board_record", "dataset", "'x'"),
        ("data_board_batch", "dataset", "'x'"),
        ("data_board_spec_version", "title", "'x'"),
    ],
)
async def test_update_is_refused_on_the_append_only_tables(
    app_session: AppSessionFactory, table: str, column: str, value: str
) -> None:
    s = await _seed(app_session)
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_session(s.tenant) as db:
            await db.execute(text(f"UPDATE {table} SET {column} = {value}"))


async def test_constraints_of_the_board_tables(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)

    async def _fails(*rows: Any) -> None:
        with pytest.raises(IntegrityError):
            async with app_session(s.tenant) as db:
                db.add_all(rows)
                await db.flush()

    def _board_row(title: str) -> m.DataBoard:
        return m.DataBoard(tenant_id=s.tenant, department_id=s.dept, title=title, spec=_SPEC)

    await _fails(_board_row(""))
    await _fails(_board_row("x" * 201))
    # (board_id, version) is unique.
    await _fails(
        m.DataBoardSpecVersion(
            tenant_id=s.tenant, board_id=s.board, version=1, title="again", spec=_SPEC
        )
    )
    batch = {"tenant_id": s.tenant, "board_id": s.board, "dataset": "sales", "as_of": _NOW}
    await _fails(m.DataBoardBatch(**batch, row_count=0))
    await _fails(m.DataBoardBatch(**batch, row_count=1, note="n" * 2001))
    # The boundaries are accepted.
    async with app_session(s.tenant) as db:
        db.add_all(
            [
                _board_row("x" * 200),
                m.DataBoardBatch(**batch, row_count=1, note="n" * 2000),
                m.DataBoardSpecVersion(
                    tenant_id=s.tenant, board_id=s.board, version=2, title="v2", spec=_SPEC
                ),
            ]
        )
        await db.flush()


async def test_remove_board_refuses_a_board_already_removed(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        board = (await db.execute(select(m.DataBoard))).scalar_one()
        await remove_board(db, board, now=_NOW)
        with pytest.raises(ValueError, match="board_already_removed"):
            await remove_board(db, board, now=_NOW + dt.timedelta(hours=1))
        assert board.removed_at == _NOW


async def test_lock_live_board_blocks_a_concurrent_writer(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        locked = await lock_live_board(db, s.board)
        assert locked is not None
        # Another transaction, as board_write does, cannot take the row while we hold it.
        with pytest.raises(DBAPIError, match="could not obtain lock"):
            async with app_session(s.tenant) as other:
                await other.execute(
                    text("SELECT id FROM data_board WHERE id = :i FOR UPDATE NOWAIT"),
                    {"i": s.board},
                )
    async with app_session(s.tenant) as db:
        board = (await db.execute(select(m.DataBoard))).scalar_one()
        await remove_board(db, board, now=_NOW)
        assert await lock_live_board(db, s.board) is None
