"""0124: board shares, member notifications, notification settings -- RLS,
the one-of CHECK, uniqueness and the unread-collapse partial index."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

import oc8.models as m
from tests.conftest import AppSessionFactory


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (board_id, department_id, member_id)."""
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Finance", frame={})
        member = m.OrgMember(tenant_id=tenant, subject="u", subject_uuid=uuid.uuid4())
        db.add_all([dept, member])
        await db.flush()
        board = m.DataBoard(tenant_id=tenant, department_id=dept.id, title="B", spec={})
        db.add(board)
        await db.flush()
        return board.id, dept.id, member.id


async def test_rls_isolates_the_new_tables(app_session: AppSessionFactory) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    board, _dept, member = await _seed(app_session, tenant_a)
    async with app_session(tenant_a) as db:
        db.add(m.DataBoardShare(tenant_id=tenant_a, board_id=board, member_id=member))
        db.add(
            m.MemberNotification(
                tenant_id=tenant_a,
                member_id=member,
                kind="board.available",
                title={"en": "t", "de": "t"},
                body={"en": "b", "de": "b"},
                link="/x",
            )
        )
        db.add(
            m.MemberNotificationSetting(
                tenant_id=tenant_a, member_id=member, kind="board.available"
            )
        )
    async with app_session(tenant_a) as db:
        assert len((await db.execute(select(m.MemberNotification))).scalars().all()) == 1
        setting = (await db.execute(select(m.MemberNotificationSetting))).scalar_one()
        assert setting.push_enabled is True
        assert setting.messenger_enabled is True
    async with app_session(tenant_b) as db:
        for model in (m.DataBoardShare, m.MemberNotification, m.MemberNotificationSetting):
            assert (await db.execute(select(model))).scalars().all() == []


async def test_share_needs_exactly_one_target(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    board, dept, member = await _seed(app_session, tenant)
    for kwargs in ({"member_id": member, "department_id": dept}, {}):
        with pytest.raises(IntegrityError):
            async with app_session(tenant) as db:
                db.add(m.DataBoardShare(tenant_id=tenant, board_id=board, **kwargs))


async def test_share_targets_are_unique_per_board(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    board, dept, member = await _seed(app_session, tenant)
    for kwargs in ({"member_id": member}, {"department_id": dept}):
        async with app_session(tenant) as db:
            db.add(m.DataBoardShare(tenant_id=tenant, board_id=board, **kwargs))
        with pytest.raises(IntegrityError):
            async with app_session(tenant) as db:
                db.add(m.DataBoardShare(tenant_id=tenant, board_id=board, **kwargs))


def _note(
    tenant: uuid.UUID, member: uuid.UUID, subject: uuid.UUID | None, read: bool = False
) -> m.MemberNotification:
    return m.MemberNotification(
        tenant_id=tenant,
        member_id=member,
        kind="run.failed",
        subject_id=subject,
        title={"en": "t", "de": "t"},
        body={"en": "b", "de": "b"},
        link="/x",
        read_at=dt.datetime.now(dt.UTC) if read else None,
    )


@pytest.mark.parametrize("subject", [uuid.uuid4(), None])
async def test_second_unread_notification_for_one_subject_is_rejected(
    app_session: AppSessionFactory, subject: uuid.UUID | None
) -> None:
    """A NULL subject collapses too (the index is NULLS NOT DISTINCT)."""
    tenant = uuid.uuid4()
    _board, _dept, member = await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(_note(tenant, member, subject))
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(_note(tenant, member, subject))


async def test_read_notifications_do_not_block_a_new_unread_one(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    _board, _dept, member = await _seed(app_session, tenant)
    subject = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(_note(tenant, member, subject, read=True))
        db.add(_note(tenant, member, subject, read=True))
        db.add(_note(tenant, member, subject))


async def test_setting_is_unique_per_member_and_kind(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    _board, _dept, member = await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(m.MemberNotificationSetting(tenant_id=tenant, member_id=member, kind="k"))
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(m.MemberNotificationSetting(tenant_id=tenant, member_id=member, kind="k"))


async def test_deleting_the_member_cascades(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    board, _dept, member = await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(m.DataBoardShare(tenant_id=tenant, board_id=board, member_id=member))
        db.add(_note(tenant, member, None))
    async with app_session(tenant) as db:
        await db.execute(text("DELETE FROM org_member WHERE id = :i"), {"i": member})
    async with app_session(tenant) as db:
        assert (await db.execute(select(m.DataBoardShare))).scalars().all() == []
        assert (await db.execute(select(m.MemberNotification))).scalars().all() == []
