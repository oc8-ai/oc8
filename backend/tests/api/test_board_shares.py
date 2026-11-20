"""Board sharing over HTTP: the share routes, the flags, and what a lost seat hides."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.authz.permissions import SEAT_VIEWER
from oc8.authz.scope import subject_uuid_for
from oc8.notifications import delivery, service
from tests.api.test_boards import _QUERY, _headers, _http, _Office, _office
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_deliver(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(delivery, "deliver", _fake_deliver)
    return calls


async def _drain() -> None:
    while service._background_tasks:
        await asyncio.gather(*list(service._background_tasks))


async def _member(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    subject: str,
    *,
    name: str = "",
    email: str | None = None,
    seat_in: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    async with app_session(tenant) as db:
        row = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=subject_uuid_for(subject),
            display_name=name,
            email=email,
        )
        db.add(row)
        await db.flush()
        seat_id = None
        if seat_in is not None:
            seat = m.OrgMemberDepartment(
                tenant_id=tenant, member_id=row.id, department_id=seat_in, seat_role=SEAT_VIEWER
            )
            db.add(seat)
            await db.flush()
            seat_id = seat.id
        return row.id, seat_id


async def _department(app_session: AppSessionFactory, tenant: uuid.UUID, name: str) -> uuid.UUID:
    async with app_session(tenant) as db:
        d = m.Department(tenant_id=tenant, name=name, frame={})
        db.add(d)
        await db.flush()
        return d.id


def _boss(o: _Office) -> dict[str, str]:
    return _headers(o.tenant, "boss", "org_admin")


async def test_a_seatless_member_reaches_a_board_shared_to_them(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    loner, _ = await _member(app_session, o.tenant, "loner", name="Lona")
    h = _headers(o.tenant, "loner", "member")
    async with _http() as c:
        before = await c.get("/api/v1/boards", headers=h)
        r = await c.post(
            f"/api/v1/boards/{o.sales_board}/shares",
            json={"memberId": str(loner)},
            headers=_boss(o),
        )
        assert r.status_code == 201, r.text
        lst = await c.get("/api/v1/boards", headers=h)
        one = await c.get(f"/api/v1/boards/{o.sales_board}", headers=h)
        q = await c.post(f"/api/v1/boards/{o.sales_board}/query", json=_QUERY, headers=h)
        other = await c.get(f"/api/v1/boards/{o.finance_board}", headers=h)
    # No seat and nothing shared: refused at the gate, as before.
    assert before.status_code == 403
    assert [b["id"] for b in lst.json()] == [str(o.sales_board)]
    assert lst.json()[0]["sharedWithMe"] is True and lst.json()[0]["mayShare"] is False
    assert one.status_code == 200 and one.json()["sharedWithMe"] is True
    assert q.status_code == 200, q.text
    assert other.status_code == 404 and "Revenue" not in other.text


async def test_flags_for_seat_holder_and_sharer(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    async with _http() as c:
        seat = await c.get("/api/v1/boards", headers=_headers(o.tenant, "fin-member", "member"))
        boss = await c.get("/api/v1/boards", headers=_boss(o))
    assert [(b["sharedWithMe"], b["mayShare"]) for b in seat.json()] == [(False, False)]
    assert all(b["sharedWithMe"] is False and b["mayShare"] is True for b in boss.json())


async def test_losing_the_seat_hides_the_department_share_and_its_notice(
    app_session: AppSessionFactory,
) -> None:
    """Review Focus 1: list, get, query and the inbox all forget the board."""
    o = await _office(app_session)
    ops = await _department(app_session, o.tenant, "Ops")
    async with app_session(o.tenant) as db:
        member_id = (
            await db.execute(
                select(m.OrgMemberDepartment.member_id).where(m.OrgMemberDepartment.id == o.seat)
            )
        ).scalar_one()
        ops_seat = m.OrgMemberDepartment(
            tenant_id=o.tenant, member_id=member_id, department_id=ops, seat_role=SEAT_VIEWER
        )
        db.add(ops_seat)
        await db.flush()
        ops_seat_id = ops_seat.id
    h = _headers(o.tenant, "fin-member", "member")
    async with _http() as c:
        r = await c.post(
            f"/api/v1/boards/{o.sales_board}/shares",
            json={"departmentId": str(ops)},
            headers=_boss(o),
        )
        assert r.status_code == 201, r.text
        await _drain()
        seen = await c.get(f"/api/v1/boards/{o.sales_board}", headers=h)
        inbox = await c.get("/api/v1/notifications", headers=h)
        assert seen.status_code == 200 and seen.json()["sharedWithMe"] is True
        assert [n["subjectId"] for n in inbox.json()["items"]] == [str(o.sales_board)]
        async with app_session(o.tenant) as db:
            row = (
                await db.execute(
                    select(m.OrgMemberDepartment).where(m.OrgMemberDepartment.id == ops_seat_id)
                )
            ).scalar_one()
            row.revoked_at = dt.datetime.now(dt.UTC)
        gone = await c.get(f"/api/v1/boards/{o.sales_board}", headers=h)
        q = await c.post(f"/api/v1/boards/{o.sales_board}/query", json=_QUERY, headers=h)
        lst = await c.get("/api/v1/boards", headers=h)
        inbox = await c.get("/api/v1/notifications", headers=h)
        unread = await c.get("/api/v1/notifications/unread-count", headers=h)
    for resp in (gone, q):
        assert resp.status_code == 404, resp.text
        assert "Pipeline" not in resp.text
    assert [b["id"] for b in lst.json()] == [str(o.finance_board)]
    assert inbox.json()["items"] == [] and inbox.json()["totalCount"] == 0
    assert unread.json()["count"] == 0


async def test_hidden_notices_keep_total_count_and_paging_consistent(
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
        now = dt.datetime.now(dt.UTC)
        # Newest first: visible, hidden (sales board), hidden and read, visible.
        for age, kind, subject, read in (
            (1, "board.available", o.finance_board, False),
            (2, "board.available", o.sales_board, False),
            (3, "board.available", o.sales_board, True),
            (4, "run.failed", uuid.uuid4(), False),
        ):
            db.add(
                m.MemberNotification(
                    tenant_id=o.tenant,
                    member_id=member_id,
                    kind=kind,
                    subject_id=subject,
                    title={"en": "t", "de": "t"},
                    body={"en": "b", "de": "b"},
                    link="/x",
                    created_at=now - dt.timedelta(minutes=age),
                    read_at=now if read else None,
                )
            )
    seen: list[str] = []
    async with _http() as c:
        cursor: str | None = None
        for _ in range(5):
            params: dict[str, Any] = {"limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = (await c.get("/api/v1/notifications", params=params, headers=h)).json()
            assert page["totalCount"] == 2
            assert len(page["items"]) == 1
            seen.extend(n["subjectId"] for n in page["items"])
            cursor = page["nextCursor"]
            if cursor is None:
                break
        unread = await c.get("/api/v1/notifications/unread-count", headers=h)
    assert len(seen) == 2 and seen[0] == str(o.finance_board)
    assert str(o.sales_board) not in seen
    assert unread.json()["count"] == 2


async def test_share_routes_round_trip_for_a_sharer(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    target, _ = await _member(app_session, o.tenant, "tgt", name="Tara", seat_in=o.sales)
    base = f"/api/v1/boards/{o.finance_board}/shares"
    async with _http() as c:
        a = await c.post(base, json={"memberId": str(target)}, headers=_boss(o))
        b = await c.post(base, json={"departmentId": str(o.sales)}, headers=_boss(o))
        dup = await c.post(base, json={"memberId": str(target)}, headers=_boss(o))
        bad = await c.post(base, json={"memberId": str(uuid.uuid4())}, headers=_boss(o))
        both = await c.post(
            base, json={"memberId": str(target), "departmentId": str(o.sales)}, headers=_boss(o)
        )
        lst = await c.get(base, headers=_boss(o))
        dele = await c.delete(f"{base}/{a.json()['id']}", headers=_boss(o))
        again = await c.delete(f"{base}/{a.json()['id']}", headers=_boss(o))
        after = await c.get(base, headers=_boss(o))
    assert a.status_code == 201, a.text
    assert a.json()["memberId"] == str(target) and a.json()["memberName"] == "Tara"
    assert b.status_code == 201 and b.json()["departmentName"] == "Sales"
    assert dup.status_code == 201 and dup.json()["id"] == a.json()["id"]
    assert bad.status_code == 422 and both.status_code == 422
    assert {s["id"] for s in lst.json()} == {a.json()["id"], b.json()["id"]}
    assert all("createdAt" in s for s in lst.json())
    assert dele.status_code == 204 and again.status_code == 404
    assert [s["id"] for s in after.json()] == [b.json()["id"]]


async def test_share_routes_403_for_a_viewer_and_404_out_of_reach(
    app_session: AppSessionFactory,
) -> None:
    o = await _office(app_session)
    target, _ = await _member(app_session, o.tenant, "tgt", name="Tara")
    h = _headers(o.tenant, "fin-member", "member")
    calls = (
        ("GET", "/shares", None),
        ("POST", "/shares", {"memberId": str(target)}),
        ("DELETE", f"/shares/{uuid.uuid4()}", None),
        ("GET", "/share-targets?q=ta", None),
    )
    async with _http() as c:
        for board, expected in ((o.finance_board, 403), (o.sales_board, 404)):
            for method, path, body in calls:
                r = await c.request(method, f"/api/v1/boards/{board}{path}", json=body, headers=h)
                assert r.status_code == expected, (method, path, r.text)
                assert "Pipeline" not in r.text and "Revenue" not in r.text
    async with app_session(o.tenant) as db:
        assert (await db.execute(select(m.DataBoardShare))).scalars().all() == []


async def test_share_targets_search_and_limit(app_session: AppSessionFactory) -> None:
    o = await _office(app_session)
    for i in range(25):
        await _member(app_session, o.tenant, f"z{i}", name=f"Zed {i}")
    await _member(app_session, o.tenant, "mail", email="ZEDDY@example.com")
    await _department(app_session, o.tenant, "Zed Department")
    archived = await _department(app_session, o.tenant, "Zed Archive")
    async with app_session(o.tenant) as db:
        d = (await db.execute(select(m.Department).where(m.Department.id == archived))).scalar_one()
        d.deleted_at = dt.datetime.now(dt.UTC)
    url = f"/api/v1/boards/{o.finance_board}/share-targets"
    async with _http() as c:
        many = await c.get(url, params={"q": "zed"}, headers=_boss(o))
        partial = [
            await c.get(url, params={"q": q}, headers=_boss(o))
            for q in ("zeddy@", "example.com", "@example.com", "zeddy@example.co")
        ]
        exact = await c.get(url, params={"q": " zeddy@EXAMPLE.com "}, headers=_boss(o))
        dept = await c.get(url, params={"q": "zed dep"}, headers=_boss(o))
        fin = await c.get(url, params={"q": "fin"}, headers=_boss(o))
    assert many.status_code == 200, many.text
    assert len(many.json()["members"]) == 20
    assert all(t["name"].startswith("Zed ") for t in many.json()["members"])
    # An email is matched only as a whole address: no enumeration by substring.
    for r in partial:
        assert r.status_code == 200 and r.json()["members"] == [], r.text
        assert "ZEDDY" not in r.text
    # The caller typed the whole address, so it may be shown back to them.
    assert [t["name"] for t in exact.json()["members"]] == ["ZEDDY@example.com"]
    assert [t["name"] for t in dept.json()["departments"]] == ["Zed Department"]
    assert dept.json()["members"] == []
    assert [t["name"] for t in fin.json()["departments"]] == ["Finance"]
    assert all(set(t) == {"id", "name"} for t in many.json()["members"])


async def test_a_nameless_member_is_masked_in_targets_and_shares(
    app_session: AppSessionFactory,
) -> None:
    """A sharer may lack member:view: no email or subject is shown that they did
    not type themselves."""
    o = await _office(app_session)
    nameless, _ = await _member(app_session, o.tenant, "subj-jane", email="jane.doe@example.com")
    base = f"/api/v1/boards/{o.finance_board}"
    async with _http() as c:
        targets = await c.get(f"{base}/share-targets", params={"q": ""}, headers=_boss(o))
        created = await c.post(f"{base}/shares", json={"memberId": str(nameless)}, headers=_boss(o))
        listed = await c.get(f"{base}/shares", headers=_boss(o))
    labels = {t["id"]: t["name"] for t in targets.json()["members"]}
    assert labels[str(nameless)] == "j…@example.com"
    assert created.json()["memberName"] == "j…@example.com"
    assert [s["memberName"] for s in listed.json()] == ["j…@example.com"]
    for r in (targets, created, listed):
        assert "jane.doe" not in r.text and "subj-jane" not in r.text


async def test_a_direct_share_grants_reading_only(app_session: AppSessionFactory) -> None:
    """Sharing grants read and query of one board: no delete, no run, no agent."""
    o = await _office(app_session)
    loner, _ = await _member(app_session, o.tenant, "loner", name="Lona")
    h = _headers(o.tenant, "loner", "member")
    async with _http() as c:
        r = await c.post(
            f"/api/v1/boards/{o.finance_board}/shares",
            json={"memberId": str(loner)},
            headers=_boss(o),
        )
        assert r.status_code == 201, r.text
        assert (await c.get(f"/api/v1/boards/{o.finance_board}", headers=h)).status_code == 200
        delete = await c.delete(
            f"/api/v1/boards/{o.finance_board}", params={"records": 2}, headers=h
        )
        run = await c.post(f"/api/v1/agents/{o.agent}/run", json={"task": "go"}, headers=h)
        agents = await c.get("/api/v1/agents", headers=h)
        agent = await c.get(f"/api/v1/agents/{o.agent}", headers=h)
    for resp in (delete, run, agents, agent):
        assert resp.status_code in (403, 404), (resp.request.url, resp.status_code, resp.text)
        assert "Reporter" not in resp.text
    async with app_session(o.tenant) as db:
        board = (
            await db.execute(select(m.DataBoard).where(m.DataBoard.id == o.finance_board))
        ).scalar_one()
        assert board.removed_at is None
        assert (await db.execute(select(m.AgentRun))).scalars().all() == []
