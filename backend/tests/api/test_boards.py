"""GET /boards, GET /boards/{id}, POST /boards/{id}/query."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_VIEWER
from oc8.authz.scope import subject_uuid_for
from oc8.boards.records import write_batch
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
_QUERY: dict[str, Any] = {
    "dataset": "sales",
    "fields": ["amount"],
    "period": {"from": "2026-03-01", "to": "2026-04-01"},
    "groupBy": None,
}


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


class _Office:
    tenant: uuid.UUID
    finance: uuid.UUID
    sales: uuid.UUID
    agent: uuid.UUID
    finance_board: uuid.UUID
    sales_board: uuid.UUID
    seat: uuid.UUID  # the member's finance seat row


async def _office(app_session: AppSessionFactory, *, member: str = "fin-member") -> _Office:
    o = _Office()
    o.tenant = uuid.uuid4()
    async with app_session(o.tenant) as db:
        fin = m.Department(tenant_id=o.tenant, name="Finance", frame={})
        sales = m.Department(tenant_id=o.tenant, name="Sales", frame={})
        db.add_all([fin, sales])
        await db.flush()
        agent = m.Agent(tenant_id=o.tenant, department_id=fin.id, name="Reporter")
        db.add(agent)
        await db.flush()
        boards = []
        for dept, title in ((fin, "Revenue"), (sales, "Pipeline")):
            board = m.DataBoard(
                tenant_id=o.tenant,
                department_id=dept.id,
                agent_id=agent.id,
                title=title,
                spec=_SPEC,
            )
            db.add(board)
            await db.flush()
            boards.append(board)
            await write_batch(
                db,
                tenant_id=o.tenant,
                agent=agent,
                board_id=board.id,
                dataset="sales",
                as_of="2026-03-01",
                rows=[{"amount": 100}, {"amount": 20}],
                note=None,
                run_id=None,
                now=dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC),
            )
        mem = m.OrgMember(tenant_id=o.tenant, subject=member, subject_uuid=subject_uuid_for(member))
        db.add(mem)
        await db.flush()
        seat = m.OrgMemberDepartment(
            tenant_id=o.tenant, member_id=mem.id, department_id=fin.id, seat_role=SEAT_VIEWER
        )
        db.add(seat)
        await db.flush()
        o.finance, o.sales, o.agent = fin.id, sales.id, agent.id
        o.finance_board, o.sales_board, o.seat = boards[0].id, boards[1].id, seat.id
    return o


async def test_org_admin_lists_boards_with_agent_and_count(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    async with _http() as c:
        r = await c.get("/api/v1/boards", headers=_headers(o.tenant, "boss", "org_admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [b["title"] for b in body] == ["Pipeline", "Revenue"]
    rev = body[1]
    assert rev["id"] == str(o.finance_board)
    assert rev["agentId"] == str(o.agent)
    assert rev["agentName"] == "Reporter"
    assert rev["stale"] is False
    assert rev["recordCount"] == 2
    assert rev["lastRecordedAt"] is not None
    assert rev["mayDelete"] is True
    assert rev["departmentId"] == str(o.finance)


async def test_seat_only_member_sees_and_queries_the_board(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    h = _headers(o.tenant, "fin-member", "member")
    async with _http() as c:
        lst = await c.get("/api/v1/boards", headers=h)
        one = await c.get(f"/api/v1/boards/{o.finance_board}", headers=h)
        q = await c.post(f"/api/v1/boards/{o.finance_board}/query", json=_QUERY, headers=h)
    assert [b["id"] for b in lst.json()] == [str(o.finance_board)]
    assert lst.json()[0]["mayDelete"] is False
    assert one.status_code == 200, one.text
    assert one.json()["spec"] == _SPEC and one.json()["specVersion"] == 1
    assert q.status_code == 200, q.text
    assert q.json()["totals"][0]["values"]["amount"] == 120


async def test_board_outside_reach_is_404_without_title(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    h = _headers(o.tenant, "fin-member", "member")
    async with _http() as c:
        get = await c.get(f"/api/v1/boards/{o.sales_board}", headers=h)
        post = await c.post(f"/api/v1/boards/{o.sales_board}/query", json=_QUERY, headers=h)
        lst = await c.get("/api/v1/boards", headers=h)
    for r in (get, post):
        assert r.status_code == 404, r.text
        assert "Pipeline" not in r.text and "Sales" not in r.text
    assert "Pipeline" not in lst.text and str(o.sales_board) not in lst.text


async def test_losing_the_seat_hides_the_board_on_the_next_request(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    h = _headers(o.tenant, "fin-member", "member")
    async with _http() as c:
        assert (await c.get(f"/api/v1/boards/{o.finance_board}", headers=h)).status_code == 200
        async with app_session(o.tenant) as db:
            seat = (
                await db.execute(
                    select(m.OrgMemberDepartment).where(m.OrgMemberDepartment.id == o.seat)
                )
            ).scalar_one()
            seat.revoked_at = dt.datetime.now(dt.UTC)
        gone = await c.get(f"/api/v1/boards/{o.finance_board}", headers=h)
        lst = await c.get("/api/v1/boards", headers=h)
    # A member with no seat left is refused at the gate (403) or sees nothing (404):
    # either way the board is not served.
    # No seat left and no tenant-wide agent:view: refused at the gate, and the
    # per-request scope means the very next request already sees it.
    assert gone.status_code == 403 and "Revenue" not in gone.text
    assert lst.status_code == 403


async def test_tenant_wide_agent_view_sees_all_boards(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with _http() as c:
        r = await c.get("/api/v1/boards", headers=_headers(o.tenant, "ops", "operator"))
    assert r.status_code == 200, r.text
    assert {b["title"] for b in r.json()} == {"Revenue", "Pipeline"}
    assert all(b["mayDelete"] is False for b in r.json())


async def test_removed_board_is_410_for_a_caller_in_reach(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with app_session(o.tenant) as db:
        board = (
            await db.execute(select(m.DataBoard).where(m.DataBoard.id == o.finance_board))
        ).scalar_one()
        board.removed_at = dt.datetime.now(dt.UTC)
    h = _headers(o.tenant, "fin-member", "member")
    async with _http() as c:
        get = await c.get(f"/api/v1/boards/{o.finance_board}", headers=h)
        post = await c.post(f"/api/v1/boards/{o.finance_board}/query", json=_QUERY, headers=h)
        lst = await c.get("/api/v1/boards", headers=h)
    assert get.status_code == 410 and post.status_code == 410
    assert lst.json() == []


async def test_query_returns_aggregates(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with _http() as c:
        r = await c.post(
            f"/api/v1/boards/{o.finance_board}/query",
            json=_QUERY,
            headers=_headers(o.tenant, "boss", "org_admin"),
        )
    assert r.status_code == 200, r.text
    assert r.json()["totals"][0]["values"]["amount"] == 120


async def test_bad_query_is_422_with_code(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with _http() as c:
        r = await c.post(
            f"/api/v1/boards/{o.finance_board}/query",
            json={**_QUERY, "fields": ["nope"]},
            headers=_headers(o.tenant, "boss", "org_admin"),
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "board_query:unknown_field"


async def test_archived_producer_marks_the_board_stale(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with app_session(o.tenant) as db:
        agent = (await db.execute(select(m.Agent).where(m.Agent.id == o.agent))).scalar_one()
        agent.deleted_at = dt.datetime.now(dt.UTC)
    async with _http() as c:
        r = await c.get(
            f"/api/v1/boards/{o.finance_board}",
            headers=_headers(o.tenant, "boss", "org_admin"),
        )
    assert r.status_code == 200, r.text
    assert r.json()["stale"] is True
