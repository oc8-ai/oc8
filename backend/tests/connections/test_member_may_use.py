"""§4: a member may use a connection when it is connected, a department they
sit in enables it, and that department grants the right."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.connections.usable import (
    direct_connection,
    member_departments_for,
    member_may_use,
    member_usable_tool_keys,
)
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import READ_ONLY, direct_office

pytestmark = pytest.mark.asyncio


async def test_a_seat_in_an_enabling_department_grants_the_frames_rights(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, policy=READ_ONLY)
        mid = office.member_id
        assert await member_may_use(db, member_id=mid, tool_key="things", right="read")
        assert not await member_may_use(db, member_id=mid, tool_key="things", right="modify")
        assert await member_usable_tool_keys(db, member_id=mid) == ["things"]


async def test_no_seat_means_no_use(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, seated=False)
        assert not await member_may_use(
            db, member_id=office.member_id, tool_key="things", right="read"
        )
        assert await member_departments_for(db, member_id=office.member_id, tool_key="things") == []


async def test_a_revoked_seat_counts_for_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        seat = (
            await db.execute(
                select(m.OrgMemberDepartment).where(
                    m.OrgMemberDepartment.member_id == office.member_id
                )
            )
        ).scalar_one()
        seat.revoked_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert not await member_may_use(
            db, member_id=office.member_id, tool_key="things", right="read"
        )


async def test_all_departments_counts_as_sitting_everywhere(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, seated=False)
        member = await db.get(m.OrgMember, office.member_id)
        assert member is not None
        member.all_departments = True
        await db.flush()
        assert await member_may_use(db, member_id=office.member_id, tool_key="things", right="read")


async def test_a_disconnected_system_is_not_usable(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, connected=False)
        assert not await member_may_use(
            db, member_id=office.member_id, tool_key="things", right="read"
        )


async def test_a_frame_that_switches_the_system_off_grants_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, policy={"enabled": False, "read": True})
        assert not await member_may_use(
            db, member_id=office.member_id, tool_key="things", right="read"
        )


async def test_the_departments_own_connection_is_the_one_used(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        conn = await direct_connection(
            db, tenant_id=tenant, tool_key="things", department_ids=[office.department_id]
        )
        assert conn is not None and conn.id == office.connection_id
