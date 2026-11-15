"""The scoped repository: a board is visible where its department is."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import oc8.models as m
from oc8.auth.principal import Principal
from oc8.authz.permissions import SEAT_VIEWER
from oc8.authz.scope import DepartmentScope, scope_for_principal, subject_uuid_for
from oc8.boards.repo import visible_board, visible_boards
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


class _World:
    finance: m.Department
    sales: m.Department
    finance_board: m.DataBoard
    sales_board: m.DataBoard


async def _world(db: AsyncSession, tenant: uuid.UUID) -> _World:
    w = _World()
    w.finance = m.Department(tenant_id=tenant, name="Finance", frame={})
    w.sales = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add_all([w.finance, w.sales])
    await db.flush()
    w.finance_board = m.DataBoard(
        tenant_id=tenant, department_id=w.finance.id, title="Alpha", spec=_SPEC
    )
    w.sales_board = m.DataBoard(
        tenant_id=tenant, department_id=w.sales.id, title="Beta", spec=_SPEC
    )
    db.add_all([w.finance_board, w.sales_board])
    await db.flush()
    return w


async def _scope(
    db: AsyncSession, tenant: uuid.UUID, subject: str, seat_in: uuid.UUID | None
) -> DepartmentScope:
    member = m.OrgMember(tenant_id=tenant, subject=subject, subject_uuid=subject_uuid_for(subject))
    db.add(member)
    await db.flush()
    if seat_in is not None:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant, member_id=member.id, department_id=seat_in, seat_role=SEAT_VIEWER
            )
        )
        await db.flush()
    _member, scope = await scope_for_principal(
        db, Principal(subject=subject, tenant_id=tenant, role="member")
    )
    return scope


async def test_a_seat_sees_only_its_departments_boards(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        scope = await _scope(db, tenant, "fin", w.finance.id)
        got = await visible_boards(db, scope=scope, tenant_wide=False, tenant_id=tenant)
        assert [b.id for b in got] == [w.finance_board.id]


async def test_no_seat_and_not_tenant_wide_sees_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _world(db, tenant)
        scope = await _scope(db, tenant, "nobody", None)
        assert await visible_boards(db, scope=scope, tenant_wide=False, tenant_id=tenant) == []


async def test_tenant_wide_sees_all(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        scope = await _scope(db, tenant, "nobody", None)
        got = await visible_boards(db, scope=scope, tenant_wide=True, tenant_id=tenant)
        assert [b.id for b in got] == [w.finance_board.id, w.sales_board.id]


async def test_archived_and_removed_boards_are_not_listed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        scope = await _scope(db, tenant, "nobody", None)
        w.finance_board.deleted_at = dt.datetime.now(dt.UTC)  # archived with its department
        w.sales_board.removed_at = dt.datetime.now(dt.UTC)  # removed by a person
        await db.flush()
        assert await visible_boards(db, scope=scope, tenant_wide=True, tenant_id=tenant) == []


async def test_visible_board_returns_removed_but_not_archived(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        scope = await _scope(db, tenant, "nobody", None)
        w.finance_board.deleted_at = dt.datetime.now(dt.UTC)
        w.sales_board.removed_at = dt.datetime.now(dt.UTC)
        await db.flush()
        archived = await visible_board(
            db, scope=scope, tenant_wide=True, tenant_id=tenant, board_id=w.finance_board.id
        )
        removed = await visible_board(
            db, scope=scope, tenant_wide=True, tenant_id=tenant, board_id=w.sales_board.id
        )
        assert archived is None
        assert removed is not None and removed.removed_at is not None


async def test_visible_board_outside_scope_is_none(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        scope = await _scope(db, tenant, "fin", w.finance.id)
        assert (
            await visible_board(
                db, scope=scope, tenant_wide=False, tenant_id=tenant, board_id=w.sales_board.id
            )
            is None
        )
        assert (
            await visible_board(
                db, scope=scope, tenant_wide=False, tenant_id=tenant, board_id=w.finance_board.id
            )
            is not None
        )
