"""§4: a member may use a connection when it is connected, a department they
sit in enables it, and that department grants the right."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import ToolPolicy
from oc8.connections.usable import (
    direct_connection,
    member_departments_for,
    member_may_use,
    member_usable_tool_keys,
)
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import (
    READ_ONLY,
    READ_WRITE,
    SCOPES,
    DirectOffice,
    direct_office,
)

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
            db,
            tenant_id=tenant,
            tool_key="things",
            departments=[(office.department_id, ToolPolicy.from_json(READ_WRITE))],
            right="read",
        )
        assert conn is not None and conn.id == office.connection_id


async def _second_department(
    db: AsyncSession,
    office: DirectOffice,
    *,
    name: str,
    policy: dict[str, Any],
    connected: bool = True,
) -> tuple[m.Department, m.McpConnection]:
    dept = m.Department(
        tenant_id=office.tenant, name=name, frame={"tools": {"things": dict(policy)}}
    )
    db.add(dept)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=office.tenant,
            member_id=office.member_id,
            department_id=dept.id,
            seat_role="dept_viewer",
        )
    )
    conn = m.McpConnection(
        tenant_id=office.tenant,
        department_id=dept.id,
        name="things",
        transport="stdio",
        server_url="stdio://things",
        connected=connected,
        config={"command": "x", "args": []},
        scopes=SCOPES,
    )
    db.add(conn)
    await db.flush()
    return dept, conn


async def test_a_call_runs_on_a_connection_of_a_department_granting_its_right(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # Sales: read-only, connection A. Support: modify, connection B.
        office = await direct_office(db, tenant, policy=READ_ONLY)
        _support, conn_b = await _second_department(db, office, name="Support", policy=READ_WRITE)
        departments = await member_departments_for(
            db, member_id=office.member_id, tool_key="things"
        )
        modify = await direct_connection(
            db, tenant_id=tenant, tool_key="things", departments=departments, right="modify"
        )
        assert modify is not None and modify.id == conn_b.id
        read = await direct_connection(
            db, tenant_id=tenant, tool_key="things", departments=departments, right="read"
        )
        # Both grant read: the oldest connection wins, deterministically.
        assert read is not None and read.id == office.connection_id


async def test_a_granting_department_without_a_connection_is_not_replaced_by_anothers(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # Sales (read-only) has the only connected row; Support grants modify
        # but its connection is down. A modify call must not run on Sales's.
        office = await direct_office(db, tenant, policy=READ_ONLY)
        await _second_department(db, office, name="Support", policy=READ_WRITE, connected=False)
        departments = await member_departments_for(
            db, member_id=office.member_id, tool_key="things"
        )
        assert (
            await direct_connection(
                db, tenant_id=tenant, tool_key="things", departments=departments, right="modify"
            )
            is None
        )


async def test_a_downgraded_frame_is_seen_by_a_session_that_already_loaded_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as first:
        office = await direct_office(first, tenant)
        await first.commit()
        await first.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
        )
        mid = office.member_id
        # Hold the rows strongly, as a run holding its agent and member does:
        # the identity map only keeps weak references.
        held = [
            await first.get(m.Department, office.department_id),
            await first.get(m.OrgMember, mid),
        ]
        assert all(row is not None for row in held)
        assert await member_may_use(first, member_id=mid, tool_key="things", right="modify")
        async with app_session(tenant) as second:
            dept = await second.get(m.Department, office.department_id)
            assert dept is not None
            dept.frame = {"tools": {"things": dict(READ_ONLY)}}
        assert not await member_may_use(first, member_id=mid, tool_key="things", right="modify")
        assert await member_may_use(first, member_id=mid, tool_key="things", right="read")
        async with app_session(tenant) as second:
            dept = await second.get(m.Department, office.department_id)
            assert dept is not None
            dept.frame = {"tools": {"things": {"enabled": False, "read": True}}}
        assert not await member_may_use(first, member_id=mid, tool_key="things", right="read")
        async with app_session(tenant) as second:
            member = await second.get(m.OrgMember, mid)
            assert member is not None
            member.deleted_at = dt.datetime.now(dt.UTC)
        assert await member_departments_for(first, member_id=mid, tool_key="things") == []
