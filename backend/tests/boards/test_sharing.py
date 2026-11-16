"""Sharing a board: who sees it through a share, who is told, what is audited."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import oc8.models as m
from oc8.authz.permissions import SEAT_APPROVER, SEAT_VIEWER
from oc8.authz.scope import scope_for_member, subject_uuid_for
from oc8.boards.repo import visible_board, visible_boards
from oc8.boards.sharing import ShareTargetInvalid, board_audience, share_board, unshare_board
from oc8.notifications import delivery, service
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


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_deliver(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(delivery, "deliver", _fake_deliver)
    return calls


async def _drain() -> None:
    while service._background_tasks:
        await asyncio.gather(*list(service._background_tasks))


class _World:
    tenant: uuid.UUID
    finance: m.Department
    sales: m.Department
    ops: m.Department
    board: m.DataBoard
    owner: m.OrgMember  # finance seat with agent_manage: may share
    fin: m.OrgMember  # finance seat
    sales_member: m.OrgMember  # sales seat
    ops_member: m.OrgMember  # ops seat
    loner: m.OrgMember  # no seat at all
    boss: m.OrgMember  # all_departments
    seats: dict[str, m.OrgMemberDepartment]


async def _member(db: AsyncSession, tenant: uuid.UUID, name: str, **kw: Any) -> m.OrgMember:
    subject = f"{name}-{uuid.uuid4().hex[:6]}"
    row = m.OrgMember(
        tenant_id=tenant,
        subject=subject,
        subject_uuid=subject_uuid_for(subject),
        display_name=name,
        **kw,
    )
    db.add(row)
    await db.flush()
    return row


async def _world(db: AsyncSession, tenant: uuid.UUID) -> _World:
    w = _World()
    w.tenant = tenant
    w.finance = m.Department(tenant_id=tenant, name="Finance", frame={})
    w.sales = m.Department(tenant_id=tenant, name="Sales", frame={})
    w.ops = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add_all([w.finance, w.sales, w.ops])
    await db.flush()
    w.board = m.DataBoard(tenant_id=tenant, department_id=w.finance.id, title="Revenue", spec=_SPEC)
    db.add(w.board)
    await db.flush()
    w.owner = await _member(db, tenant, "Owner")
    w.fin = await _member(db, tenant, "Fiona")
    w.sales_member = await _member(db, tenant, "Sam")
    w.ops_member = await _member(db, tenant, "Otto")
    w.loner = await _member(db, tenant, "Lona")
    w.boss = await _member(db, tenant, "Boss", all_departments=True)
    w.seats = {}
    for key, member, dept, role, manage in (
        ("owner", w.owner, w.finance, SEAT_APPROVER, True),
        ("fin", w.fin, w.finance, SEAT_VIEWER, False),
        ("sales", w.sales_member, w.sales, SEAT_VIEWER, False),
        ("ops", w.ops_member, w.ops, SEAT_VIEWER, False),
    ):
        seat = m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=member.id,
            department_id=dept.id,
            seat_role=role,
            agent_manage=manage,
        )
        db.add(seat)
        await db.flush()
        w.seats[key] = seat
    return w


async def _sees(
    db: AsyncSession, w: _World, member: m.OrgMember, *, with_member: bool = True
) -> bool:
    """Both repository doors must agree, for list and for one board."""
    scope = await scope_for_member(db, member, token_role="member")
    member_id = member.id if with_member else None
    listed = await visible_boards(
        db, scope=scope, tenant_wide=False, tenant_id=w.tenant, member_id=member_id
    )
    one = await visible_board(
        db,
        scope=scope,
        tenant_wide=False,
        tenant_id=w.tenant,
        board_id=w.board.id,
        member_id=member_id,
    )
    in_list = w.board.id in {b.id for b in listed}
    assert in_list == (one is not None), "list and get disagree"
    return in_list


def _share(db: AsyncSession, w: _World, **target: uuid.UUID) -> m.DataBoardShare:
    row = m.DataBoardShare(tenant_id=w.tenant, board_id=w.board.id, created_by=w.owner.id, **target)
    db.add(row)
    return row


# ------------------------------------------------------------ visibility matrix


async def test_a_seat_in_the_boards_department_sees_it(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        assert await _sees(db, w, w.fin)
        assert not await _sees(db, w, w.sales_member)


async def test_a_direct_share_shows_it_even_without_any_seat(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        assert not await _sees(db, w, w.loner)
        _share(db, w, member_id=w.loner.id)
        await db.flush()
        assert await _sees(db, w, w.loner)
        # Without member_id (the builder's "dashboards you may change"), a share
        # counts for nothing.
        assert not await _sees(db, w, w.loner, with_member=False)


async def test_a_department_share_shows_it_to_that_departments_seats(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        _share(db, w, department_id=w.sales.id)
        await db.flush()
        assert await _sees(db, w, w.sales_member)
        assert not await _sees(db, w, w.sales_member, with_member=False)
        assert not await _sees(db, w, w.ops_member)


async def test_losing_the_seat_loses_the_department_share(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        _share(db, w, department_id=w.sales.id)
        await db.flush()
        assert await _sees(db, w, w.sales_member)
        w.seats["sales"].revoked_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert not await _sees(db, w, w.sales_member)


async def test_an_archived_shared_department_shares_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        _share(db, w, department_id=w.sales.id)
        w.sales.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert not await _sees(db, w, w.sales_member)


async def test_an_archived_board_department_hides_it_even_when_shared(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        _share(db, w, member_id=w.loner.id)
        _share(db, w, department_id=w.sales.id)
        w.finance.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert not await _sees(db, w, w.loner)
        assert not await _sees(db, w, w.sales_member)


# ------------------------------------------------------------ audience and share


async def test_audience_is_seats_everywhere_members_and_shares(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        gone = await _member(db, tenant, "Gone")
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=gone.id,
                department_id=w.finance.id,
                seat_role=SEAT_VIEWER,
            )
        )
        gone.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert await board_audience(db, w.board) == {w.owner.id, w.fin.id, w.boss.id}
        _share(db, w, member_id=w.loner.id)
        _share(db, w, department_id=w.sales.id)
        w.seats["ops"].revoked_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert await board_audience(db, w.board) == {
            w.owner.id,
            w.fin.id,
            w.boss.id,
            w.loner.id,
            w.sales_member.id,
        }


async def _notices(db: AsyncSession, board_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (
        await db.execute(
            select(m.MemberNotification.member_id).where(
                m.MemberNotification.kind == "board.available",
                m.MemberNotification.subject_id == board_id,
            )
        )
    ).scalars()
    return set(rows)


async def _audits(db: AsyncSession, action: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(select(m.AuditEvent.resource).where(m.AuditEvent.action == action))
    ).scalars()
    return list(rows)


async def test_sharing_tells_exactly_the_newly_seeing_and_audits_ids(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        # The owner shares to Sales; Fiona (already sees) and Sam's new seat-mate
        # in Finance must not be told, and neither must the owner.
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=w.fin.id,
                department_id=w.sales.id,
                seat_role=SEAT_VIEWER,
            )
        )
        await db.flush()
        share = await share_board(
            db, board=w.board, actor_member_id=w.owner.id, department_id=w.sales.id
        )
        assert share.department_id == w.sales.id and share.member_id is None
        ids = (w.tenant, w.board.id, w.sales.id, share.id)
    await _drain()
    async with app_session(tenant) as db:
        assert await _notices(db, w.board.id) == {w.sales_member.id}
        (audit,) = await _audits(db, "board.shared")
        assert audit == {"board_id": str(ids[1]), "department_id": str(ids[2])}
        # Sharing again returns the same row and tells nobody twice.
        again = await share_board(
            db, board=w.board, actor_member_id=w.owner.id, department_id=w.sales.id
        )
        assert again.id == ids[3]
        # The actor never tells themself, even through a direct share.
        await share_board(db, board=w.board, actor_member_id=w.loner.id, member_id=w.loner.id)
    await _drain()
    async with app_session(tenant) as db:
        assert await _notices(db, w.board.id) == {w.sales_member.id}
    assert all(call["kind"] == "board.available" for call in delivered)


async def test_share_title_is_localized_and_links_to_the_workspace(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        await share_board(db, board=w.board, actor_member_id=w.owner.id, member_id=w.loner.id)
    await _drain()
    async with app_session(tenant) as db:
        row = (
            await db.execute(
                select(m.MemberNotification).where(m.MemberNotification.member_id == w.loner.id)
            )
        ).scalar_one()
    assert row.link == "/workspace" and row.subject_id == w.board.id
    assert row.title == {"en": "Dashboard shared with you", "de": "Dashboard mit dir geteilt"}
    assert row.body == {"en": "Revenue", "de": "Revenue"}


async def test_share_target_must_be_a_live_member_or_department(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        w.loner.deleted_at = dt.datetime.now(dt.UTC)
        w.ops.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        for target in (
            {"member_id": w.loner.id},
            {"member_id": uuid.uuid4()},
            {"department_id": w.ops.id},
            {"department_id": uuid.uuid4()},
            {},
            {"member_id": w.fin.id, "department_id": w.sales.id},
        ):
            with pytest.raises(ShareTargetInvalid):
                await share_board(db, board=w.board, actor_member_id=w.owner.id, **target)


async def test_unsharing_removes_the_share_and_audits_ids(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        share = await share_board(
            db, board=w.board, actor_member_id=w.owner.id, member_id=w.loner.id
        )
        share_id = share.id
    await _drain()
    async with app_session(tenant) as db:
        board = (
            await db.execute(select(m.DataBoard).where(m.DataBoard.id == w.board.id))
        ).scalar_one()
        await unshare_board(db, board=board, share_id=share_id, actor_member_id=w.owner.id)
    async with app_session(tenant) as db:
        assert (
            await db.execute(select(m.DataBoardShare).where(m.DataBoardShare.id == share_id))
        ).scalar_one_or_none() is None
        (audit,) = await _audits(db, "board.unshared")
        assert audit == {"board_id": str(w.board.id), "member_id": str(w.loner.id)}
        assert not await _sees(db, w, w.loner)
