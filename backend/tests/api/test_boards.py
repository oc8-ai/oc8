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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import authorize_agent_write
from oc8.api.v1 import boards as boards_api
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
    # No seat left and no tenant-wide agent:view: refused at the gate, and the
    # per-request scope means the very next request already sees it.
    assert gone.status_code == 403 and "Revenue" not in gone.text
    assert lst.status_code == 403


async def test_revoking_one_of_two_seats_hides_only_that_departments_board(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    h = _headers(o.tenant, "fin-member", "member")
    async with app_session(o.tenant) as db:
        member_id = (
            await db.execute(
                select(m.OrgMemberDepartment.member_id).where(m.OrgMemberDepartment.id == o.seat)
            )
        ).scalar_one()
        sales_seat = m.OrgMemberDepartment(
            tenant_id=o.tenant,
            member_id=member_id,
            department_id=o.sales,
            seat_role=SEAT_VIEWER,
        )
        db.add(sales_seat)
        await db.flush()
        sales_seat_id = sales_seat.id
    async with _http() as c:
        assert (await c.get(f"/api/v1/boards/{o.sales_board}", headers=h)).status_code == 200
        async with app_session(o.tenant) as db:
            row = (
                await db.execute(
                    select(m.OrgMemberDepartment).where(m.OrgMemberDepartment.id == sales_seat_id)
                )
            ).scalar_one()
            row.revoked_at = dt.datetime.now(dt.UTC)
        gone = await c.get(f"/api/v1/boards/{o.sales_board}", headers=h)
        lst = await c.get("/api/v1/boards", headers=h)
        fin = await c.get(f"/api/v1/boards/{o.finance_board}", headers=h)
    assert gone.status_code == 404, gone.text
    assert "Pipeline" not in gone.text
    assert [b["id"] for b in lst.json()] == [str(o.finance_board)]
    assert fin.status_code == 200, fin.text


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


async def test_a_compare_before_the_first_date_is_422_invalid_period(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    async with _http() as c:
        r = await c.post(
            f"/api/v1/boards/{o.finance_board}/query",
            json={
                **_QUERY,
                "period": {"from": "0001-01-01", "to": "0001-02-01"},
                "compare": "same_period_last_year",
            },
            headers=_headers(o.tenant, "boss", "org_admin"),
        )
    assert r.status_code == 422, r.text
    assert r.json() == {"detail": "board_query:invalid_period"}


async def test_unbounded_filters_are_422(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with _http() as c:
        r = await c.post(
            f"/api/v1/boards/{o.finance_board}/query",
            json={**_QUERY, "filters": {f"k{i}": ["v"] for i in range(11)}},
            headers=_headers(o.tenant, "boss", "org_admin"),
        )
    # Refused by the body's bounds, before the query reads the board.
    assert r.status_code == 422, r.text
    assert [e["type"] for e in r.json()["detail"]] == ["too_long"]


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


async def _count(app_session: AppSessionFactory, tenant: uuid.UUID, model: Any) -> int:
    async with app_session(tenant) as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def test_delete_requires_the_confirmed_record_count(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    h = _headers(o.tenant, "boss", "org_admin")
    url = f"/api/v1/boards/{o.finance_board}"
    async with _http() as c:
        stale = await c.delete(url, params={"records": 1}, headers=h)
        assert stale.status_code == 409 and stale.json()["detail"] == "board_records_changed"
        assert (await c.get(url, headers=h)).status_code == 200
        ok = await c.delete(url, params={"records": 2}, headers=h)
        assert ok.status_code == 204, ok.text
        assert (await c.get(url, headers=h)).status_code == 410
        again = await c.delete(url, params={"records": 0}, headers=h)
        assert again.status_code == 404
    async with app_session(o.tenant) as db:
        for model in (m.DataBoardRecord, m.DataBoardBatch):
            n = (
                await db.execute(
                    select(func.count()).select_from(model).where(model.board_id == o.finance_board)
                )
            ).scalar_one()
            assert n == 0
        other = (
            await db.execute(
                select(func.count())
                .select_from(m.DataBoardRecord)
                .where(m.DataBoardRecord.board_id == o.sales_board)
            )
        ).scalar_one()
        assert other == 2  # the other department's board is untouched


@pytest.mark.parametrize("column", ["deleted_at", "removed_at"])
async def test_delete_of_a_board_archived_or_removed_meanwhile_is_404(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, column: str
) -> None:
    """The board is archived with its department (or removed) after the route
    read it but before it locks it: the lock reads it again, finds nothing live,
    and answers a value-free 404 -- nothing is deleted."""
    o = await _office(app_session)

    async def racing(request: Any, db: AsyncSession, *args: Any, **kwargs: Any) -> None:
        await authorize_agent_write(request, db, *args, **kwargs)
        await db.execute(
            update(m.DataBoard)
            .where(m.DataBoard.id == o.finance_board)
            .values({column: dt.datetime.now(dt.UTC)})
        )

    monkeypatch.setattr(boards_api, "authorize_agent_write", racing)
    h = _headers(o.tenant, "boss", "org_admin")
    async with _http() as c:
        r = await c.delete(f"/api/v1/boards/{o.finance_board}", params={"records": 2}, headers=h)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "board not found"}
    async with app_session(o.tenant) as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(m.DataBoardRecord)
                .where(m.DataBoardRecord.board_id == o.finance_board)
            )
        ).scalar_one()
        assert n == 2


async def test_delete_needs_agent_manage_in_the_department(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    async with _http() as c:
        # An operator holds agent:view tenant-wide but not agent:manage: refused at the door.
        op = await c.delete(
            f"/api/v1/boards/{o.finance_board}",
            params={"records": 2},
            headers=_headers(o.tenant, "ops", "operator"),
        )
        assert op.status_code == 403, op.text
        # A seat with agent_manage in its OWN department only.
        async with app_session(o.tenant) as db:
            seat = (
                await db.execute(
                    select(m.OrgMemberDepartment).where(m.OrgMemberDepartment.id == o.seat)
                )
            ).scalar_one()
            seat.agent_manage = True
        h = _headers(o.tenant, "fin-member", "member")
        foreign = await c.delete(
            f"/api/v1/boards/{o.sales_board}", params={"records": 2}, headers=h
        )
        assert foreign.status_code == 404, foreign.text
        assert "Pipeline" not in foreign.text and "Sales" not in foreign.text
        own = await c.delete(f"/api/v1/boards/{o.finance_board}", params={"records": 2}, headers=h)
        assert own.status_code == 204, own.text
    assert await _count(app_session, o.tenant, m.DataBoardRecord) == 2  # sales board's rows


async def test_delete_is_audited(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with _http() as c:
        r = await c.delete(
            f"/api/v1/boards/{o.finance_board}",
            params={"records": 2},
            headers=_headers(o.tenant, "boss", "org_admin"),
        )
    assert r.status_code == 204, r.text
    async with app_session(o.tenant) as db:
        events = (
            (await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == "board.deleted")))
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].resource["record_count"] == 2
    assert events[0].resource["board_id"] == str(o.finance_board)
    assert events[0].resource["department_id"] == str(o.finance)
    assert events[0].resource["by"] == "boss"
