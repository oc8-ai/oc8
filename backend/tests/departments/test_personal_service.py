"""`oc8.departments.personal`: the tenant defaults and lazy creation of a
member's personal department.

Every test mints its own tenant: `org_member` carries `UNIQUE (tenant_id,
subject)` and nothing here rolls back between tests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.scope import subject_uuid_for
from oc8.departments.personal import (
    BUDGET_KEY,
    ENABLED_KEY,
    FRAME_KEY,
    PersonalDefaults,
    PersonalDepartmentsDisabled,
    follow_default,
    get_or_create_personal_department,
    owned_personal_department,
    personal_defaults,
    personal_frame,
    resync_frames,
)
from oc8.metering.budget import get_budget
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _tenant(
    app_session: AppSessionFactory,
    settings: dict[str, Any] | None,
    *,
    display_name: str = "Ada Lovelace",
) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh tenant (with an org row carrying `settings`, or none at all when
    `settings` is None) and one member in it. Committed, so independent
    sessions see both."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        if settings is not None:
            db.add(
                m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings=settings)
            )
        subject = f"user-{uuid.uuid4().hex[:8]}"
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=subject_uuid_for(subject),
            display_name=display_name,
        )
        db.add(member)
        await db.flush()
        return tenant, member.id


async def _member(db: AsyncSession, member_id: uuid.UUID) -> m.OrgMember:
    member = await db.get(m.OrgMember, member_id)
    assert member is not None
    return member


async def _live_seats(
    db: AsyncSession, tenant: uuid.UUID, member_id: uuid.UUID
) -> list[m.OrgMemberDepartment]:
    return list(
        (
            await db.execute(
                select(m.OrgMemberDepartment).where(
                    m.OrgMemberDepartment.tenant_id == tenant,
                    m.OrgMemberDepartment.member_id == member_id,
                    m.OrgMemberDepartment.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def test_defaults_when_nothing_is_set(app_session: AppSessionFactory) -> None:
    # PR 3 switched the default on (spec A1): now that a personal department's
    # content is private to its owner, an absent key reads as enabled.
    tenant, _ = await _tenant(app_session, {})
    async with app_session(tenant) as db:
        d = await personal_defaults(db, tenant_id=tenant)
    assert d == PersonalDefaults(
        enabled=True, frame_tools={}, soft_limit_tokens=None, hard_limit_tokens=None
    )


async def test_absent_key_reads_as_enabled(app_session: AppSessionFactory) -> None:
    tenant, _ = await _tenant(app_session, {FRAME_KEY: {"x": {"enabled": True}}})
    async with app_session(tenant) as db:
        assert (await personal_defaults(db, tenant_id=tenant)).enabled is True


async def test_explicit_false_reads_as_disabled(app_session: AppSessionFactory) -> None:
    # An administrator who switched the feature off keeps it off.
    tenant, _ = await _tenant(app_session, {ENABLED_KEY: False})
    async with app_session(tenant) as db:
        assert (await personal_defaults(db, tenant_id=tenant)).enabled is False


async def test_defaults_without_an_org_row(app_session: AppSessionFactory) -> None:
    tenant, _ = await _tenant(app_session, None)
    async with app_session(tenant) as db:
        d = await personal_defaults(db, tenant_id=tenant)
    assert d == PersonalDefaults(
        enabled=True, frame_tools={}, soft_limit_tokens=None, hard_limit_tokens=None
    )


async def test_malformed_values_read_as_the_defaults(app_session: AppSessionFactory) -> None:
    tenant, _ = await _tenant(
        app_session,
        {
            ENABLED_KEY: "yes",
            FRAME_KEY: ["not", "a", "dict"],
            BUDGET_KEY: {"soft_limit_tokens": True, "hard_limit_tokens": -5},
        },
    )
    async with app_session(tenant) as db:
        d = await personal_defaults(db, tenant_id=tenant)
    # A malformed switch reads as the default (on); only an explicit `false`
    # turns the feature off.
    assert d == PersonalDefaults(
        enabled=True, frame_tools={}, soft_limit_tokens=None, hard_limit_tokens=None
    )


async def test_stored_values_are_read(app_session: AppSessionFactory) -> None:
    tools = {"x": {"enabled": True, "read": True}}
    tenant, _ = await _tenant(
        app_session,
        {
            ENABLED_KEY: True,
            FRAME_KEY: tools,
            BUDGET_KEY: {"soft_limit_tokens": 1000, "hard_limit_tokens": None},
        },
    )
    async with app_session(tenant) as db:
        d = await personal_defaults(db, tenant_id=tenant)
    assert d == PersonalDefaults(
        enabled=True, frame_tools=tools, soft_limit_tokens=1000, hard_limit_tokens=None
    )


async def test_personal_frame_shape() -> None:
    tools = {"x": {"enabled": True}}
    frame = personal_frame(tools)
    assert frame == {
        "closed": True,
        "tools": tools,
        "kbs": [],
        "memory": {"department": ["read", "write"]},
    }
    # A copy, so editing a department's frame never edits the tenant default.
    frame["tools"]["x"]["enabled"] = False
    assert tools == {"x": {"enabled": True}}


async def test_first_use_creates_department_seat_frame_and_budget(
    app_session: AppSessionFactory,
) -> None:
    tenant, member_id = await _tenant(
        app_session,
        {
            ENABLED_KEY: True,
            FRAME_KEY: {"x": {"enabled": True, "read": True}},
            BUDGET_KEY: {"soft_limit_tokens": 1000, "hard_limit_tokens": 2000},
        },
    )
    async with app_session(tenant) as db:
        member = await _member(db, member_id)
        dept = await get_or_create_personal_department(db, member=member)
        assert dept.kind == DEPARTMENT_KIND_PERSONAL
        assert dept.owner_member_id == member.id
        assert dept.name == member.display_name
        assert dept.presentation == {"icon": "user"}
        assert dept.frame == {
            "tools": {"x": {"enabled": True, "read": True}},
            "closed": True,
            "kbs": [],
            "memory": {"department": ["read", "write"]},
        }
        seats = await _live_seats(db, tenant, member.id)
        assert len(seats) == 1
        seat = seats[0]
        assert seat.department_id == dept.id
        assert (seat.seat_role, seat.agent_manage, seat.granted_by) == (
            "dept_approver",
            True,
            member.id,
        )
        budget = await get_budget(db, tenant_id=tenant, department_id=dept.id)
        assert budget is not None
        assert (budget.soft_limit_tokens, budget.hard_limit_tokens) == (1000, 2000)
        again = await get_or_create_personal_department(db, member=member)
        assert again.id == dept.id
        assert len(await _live_seats(db, tenant, member.id)) == 1


async def test_name_falls_back_to_the_subject(app_session: AppSessionFactory) -> None:
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: True}, display_name="")
    async with app_session(tenant) as db:
        member = await _member(db, member_id)
        dept = await get_or_create_personal_department(db, member=member)
        assert dept.name == member.subject


async def test_no_budget_row_when_the_default_has_no_limits(
    app_session: AppSessionFactory,
) -> None:
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: True})
    async with app_session(tenant) as db:
        member = await _member(db, member_id)
        dept = await get_or_create_personal_department(db, member=member)
        assert await get_budget(db, tenant_id=tenant, department_id=dept.id) is None


async def test_disabled_refuses_creation(app_session: AppSessionFactory) -> None:
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: False})
    async with app_session(tenant) as db:
        member = await _member(db, member_id)
        with pytest.raises(PersonalDepartmentsDisabled):
            await get_or_create_personal_department(db, member=member)
        assert await owned_personal_department(db, tenant_id=tenant, member_id=member_id) is None


async def test_absent_enabled_key_allows_creation(app_session: AppSessionFactory) -> None:
    # On by default since PR 3 (spec A1).
    tenant, member_id = await _tenant(app_session, {})
    async with app_session(tenant) as db:
        dept = await get_or_create_personal_department(db, member=await _member(db, member_id))
        assert dept.kind == DEPARTMENT_KIND_PERSONAL


async def test_explicit_false_refuses_creation(app_session: AppSessionFactory) -> None:
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: False})
    async with app_session(tenant) as db:
        member = await _member(db, member_id)
        with pytest.raises(PersonalDepartmentsDisabled):
            await get_or_create_personal_department(db, member=member)


async def test_disabling_keeps_an_existing_department(app_session: AppSessionFactory) -> None:
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: True})
    async with app_session(tenant) as db:
        dept = await get_or_create_personal_department(db, member=await _member(db, member_id))
        dept_id = dept.id
    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        assert org is not None
        org.settings = {**org.settings, ENABLED_KEY: False}
    async with app_session(tenant) as db:
        owned = await owned_personal_department(db, tenant_id=tenant, member_id=member_id)
        assert owned is not None and owned.id == dept_id
        member = await _member(db, member_id)
        assert (await get_or_create_personal_department(db, member=member)).id == dept_id


async def test_a_deleted_department_is_not_owned(app_session: AppSessionFactory) -> None:
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: True})
    async with app_session(tenant) as db:
        dept = await get_or_create_personal_department(db, member=await _member(db, member_id))
        dept.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert await owned_personal_department(db, tenant_id=tenant, member_id=member_id) is None


async def test_concurrent_first_use_creates_one_department(
    app_session: AppSessionFactory,
) -> None:
    """Two tabs, two transactions, one member's first hire (Review Focus 1).

    The second transaction arrives while the first is still open, the only
    ordering that exercises the conflict: its INSERT waits on the partial
    unique index, then loses and must adopt the winner instead of a 500.
    """
    tenant, member_id = await _tenant(
        app_session,
        {ENABLED_KEY: True, BUDGET_KEY: {"soft_limit_tokens": 10, "hard_limit_tokens": 20}},
    )
    first_is_in = asyncio.Event()

    async def _first() -> uuid.UUID:
        async with app_session(tenant) as db:
            dept = await get_or_create_personal_department(db, member=await _member(db, member_id))
            first_is_in.set()
            # Still uncommitted while the second call runs into it.
            await asyncio.sleep(0.2)
            return dept.id

    async def _second() -> uuid.UUID:
        await first_is_in.wait()
        async with app_session(tenant) as db:
            dept = await get_or_create_personal_department(db, member=await _member(db, member_id))
            return dept.id

    one, two = await asyncio.wait_for(asyncio.gather(_first(), _second()), timeout=30)
    assert one == two, "two tabs created two personal departments for one member"

    async with app_session(tenant) as db:
        departments = (
            (
                await db.execute(
                    select(m.Department).where(
                        m.Department.tenant_id == tenant,
                        m.Department.kind == DEPARTMENT_KIND_PERSONAL,
                        m.Department.owner_member_id == member_id,
                        m.Department.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(departments) == 1
        assert len(await _live_seats(db, tenant, member_id)) == 1
        budgets = (
            (await db.execute(select(m.Budget).where(m.Budget.tenant_id == tenant))).scalars().all()
        )
        assert len(budgets) == 1


@pytest.mark.parametrize("how", ["resync", "follow_default"])
async def test_reapplying_the_default_frame_replaces_only_its_tools(
    app_session: AppSessionFactory, how: str
) -> None:
    """Final review M2: a re-sync or follow-default rebuilt the whole frame from
    `personal_frame`, dropping every other key a department's frame carries.
    It now swaps `tools` alone and keeps the frame closed."""
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: True, FRAME_KEY: {}})
    connection = str(uuid.uuid4())
    async with app_session(tenant) as db:
        dept = await get_or_create_personal_department(db, member=await _member(db, member_id))
        dept.frame = {
            **(dept.frame or {}),
            "emits": ["note.created"],
            "intakes": ["request"],
            "default_connection_id": connection,
        }
        if how == "follow_default":
            dept.frame_overridden_at = dt.datetime.now(dt.UTC)
        await db.flush()
        new_tools = {"docs.files": {"enabled": True, "read": True}}
        if how == "resync":
            assert await resync_frames(db, tenant_id=tenant, new_tools=new_tools) == 1
        else:
            org = await db.get(m.Organization, tenant)
            assert org is not None
            org.settings = {**org.settings, FRAME_KEY: new_tools}
            await db.flush()
            assert await follow_default(db, tenant_id=tenant, department_id=dept.id) is not None
        await db.flush()
        await db.refresh(dept)
        assert dept.frame["tools"] == new_tools
        assert dept.frame["closed"] is True
        assert dept.frame["emits"] == ["note.created"]
        assert dept.frame["intakes"] == ["request"]
        assert dept.frame["default_connection_id"] == connection
        assert dept.frame["memory"] == {"department": ["read", "write"]}


@pytest.mark.parametrize("how", ["created", "resync", "follow_default"])
async def test_the_personal_frame_never_grants_the_company_memory_tier(
    app_session: AppSessionFactory, how: str
) -> None:
    """Spec A4: a personal agent's memory is its department's own. The frame a
    personal department starts with, and the one a re-sync or follow-default
    copies over it, grants no `company` tier -- so nothing the agent recalls
    or writes goes through the company store other agents read."""
    assert "company" not in personal_frame({"x": {"enabled": True}})["memory"]
    tenant, member_id = await _tenant(app_session, {ENABLED_KEY: True, FRAME_KEY: {}})
    async with app_session(tenant) as db:
        dept = await get_or_create_personal_department(db, member=await _member(db, member_id))
        new_tools = {"docs.files": {"enabled": True, "read": True}}
        if how == "resync":
            assert await resync_frames(db, tenant_id=tenant, new_tools=new_tools) == 1
        elif how == "follow_default":
            dept.frame_overridden_at = dt.datetime.now(dt.UTC)
            org = await db.get(m.Organization, tenant)
            assert org is not None
            org.settings = {**org.settings, FRAME_KEY: new_tools}
            await db.flush()
            assert await follow_default(db, tenant_id=tenant, department_id=dept.id) is not None
        await db.flush()
        await db.refresh(dept)
        memory = (dept.frame or {}).get("memory")
        assert isinstance(memory, dict)
        assert "company" not in memory
        assert memory == {"department": ["read", "write"]}
