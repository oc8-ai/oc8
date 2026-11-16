# backend/tests/notifications/test_service.py
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.notifications import delivery, service
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _seed_members(
    app_session: AppSessionFactory, tenant: uuid.UUID, n: int
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with app_session(tenant) as db:
        for _ in range(n):
            member = m.OrgMember(
                tenant_id=tenant, subject=f"u-{uuid.uuid4().hex}", subject_uuid=uuid.uuid4()
            )
            db.add(member)
            await db.flush()
            ids.append(member.id)
    return ids


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


async def _notify(
    db: Any, tenant: uuid.UUID, members: list[uuid.UUID], subject: uuid.UUID | None
) -> None:
    await service.notify(
        db,
        tenant_id=tenant,
        member_ids=members,
        kind="run.failed",
        subject_id=subject,
        title=("Run failed", "Lauf fehlgeschlagen"),
        body=("Agent X failed", "Agent X ist fehlgeschlagen"),
        link="/runs/1",
    )


async def _rows(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.MemberNotification]:
    async with app_session(tenant) as db:
        return list((await db.execute(select(m.MemberNotification))).scalars().all())


async def test_notify_writes_one_row_per_member(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    members = await _seed_members(app_session, tenant, 2)
    subject = uuid.uuid4()
    async with app_session(tenant) as db:
        await _notify(db, tenant, members, subject)
    await _drain()
    rows = await _rows(app_session, tenant)
    assert sorted(r.member_id for r in rows) == sorted(members)
    row = rows[0]
    assert row.kind == "run.failed"
    assert row.subject_id == subject
    assert row.title == {"en": "Run failed", "de": "Lauf fehlgeschlagen"}
    assert row.body == {"en": "Agent X failed", "de": "Agent X ist fehlgeschlagen"}
    assert row.link == "/runs/1"
    assert row.read_at is None


@pytest.mark.parametrize("with_subject", [True, False])
async def test_second_notify_for_unread_row_collapses(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]], with_subject: bool
) -> None:
    tenant = uuid.uuid4()
    members = await _seed_members(app_session, tenant, 1)
    subject = uuid.uuid4() if with_subject else None
    async with app_session(tenant) as db:
        await _notify(db, tenant, members, subject)
    first = (await _rows(app_session, tenant))[0]
    async with app_session(tenant) as db:
        await service.notify(
            db,
            tenant_id=tenant,
            member_ids=members,
            kind="run.failed",
            subject_id=subject,
            title=("Run failed again", "Lauf erneut fehlgeschlagen"),
            body=("b", "b"),
            link="/runs/2",
        )
    await _drain()
    rows = await _rows(app_session, tenant)
    assert len(rows) == 1
    assert rows[0].id == first.id
    assert rows[0].created_at > first.created_at
    assert rows[0].title["en"] == "Run failed again"
    assert rows[0].link == "/runs/2"


async def test_read_row_does_not_collapse(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    members = await _seed_members(app_session, tenant, 1)
    subject = uuid.uuid4()
    async with app_session(tenant) as db:
        await _notify(db, tenant, members, subject)
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.MemberNotification))).scalar_one()
        row.read_at = row.created_at
    async with app_session(tenant) as db:
        await _notify(db, tenant, members, subject)
    await _drain()
    assert len(await _rows(app_session, tenant)) == 2


async def test_nothing_is_delivered_before_commit_and_once_after(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    members = await _seed_members(app_session, tenant, 2)
    async with app_session(tenant) as db:
        await _notify(db, tenant, members, uuid.uuid4())
        await db.flush()
        await asyncio.sleep(0)
        assert service._background_tasks == set()
        assert delivered == []
    await _drain()
    assert len(delivered) == 1
    call = delivered[0]
    assert call["tenant_id"] == tenant
    assert sorted(call["member_ids"]) == sorted(members)
    assert call["kind"] == "run.failed"
    assert call["title"] == ("Run failed", "Lauf fehlgeschlagen")
    assert call["link"] == "/runs/1"
    assert call["classification"] == "internal"


async def test_rollback_delivers_nothing(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    members = await _seed_members(app_session, tenant, 1)
    with pytest.raises(RuntimeError):
        async with app_session(tenant) as db:
            await _notify(db, tenant, members, uuid.uuid4())
            raise RuntimeError("business failure after notify")
    # The session is reused after an explicit rollback: the staged delivery
    # must not survive into the next commit either.
    async with app_session(tenant) as db:
        await _notify(db, tenant, members, uuid.uuid4())
        await db.rollback()
    await _drain()
    assert delivered == []
    assert await _rows(app_session, tenant) == []


async def test_rolled_back_savepoint_delivers_nothing(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    members = await _seed_members(app_session, tenant, 2)
    async with app_session(tenant) as db:
        await _notify(db, tenant, [members[0]], uuid.uuid4())
        nested = await db.begin_nested()
        await _notify(db, tenant, [members[1]], uuid.uuid4())
        await nested.rollback()
        # A released savepoint is not the commit: delivery waits for the root.
        async with db.begin_nested():
            await _notify(db, tenant, [members[0]], uuid.uuid4())
        await asyncio.sleep(0)
        assert delivered == [] and service._background_tasks == set()
    await _drain()
    assert len(delivered) == 2
    assert all(c["member_ids"] == [members[0]] for c in delivered)


async def test_notify_without_members_is_a_noop(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _notify(db, tenant, [], uuid.uuid4())
    await _drain()
    assert delivered == []
