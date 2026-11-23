"""Personal departments: no extra seats, no rename, and admin edits to the
frame or budget are stamped as overrides."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.scope import subject_uuid_for
from oc8.departments.personal import (
    ENABLED_KEY,
    PersonalDepartmentSeatRefused,
    get_or_create_personal_department,
)
from oc8.main import create_app
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from oc8.workspace.members import grant_seat
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="admin", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


class _World:
    def __init__(
        self,
        tenant: uuid.UUID,
        owner: uuid.UUID,
        other: uuid.UUID,
        personal: uuid.UUID,
        standard: uuid.UUID,
    ) -> None:
        self.tenant = tenant
        self.owner = owner
        self.other = other
        self.personal = personal
        self.standard = standard


async def _world(app_session: AppSessionFactory) -> _World:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t-{tenant.hex[:8]}",
                name="T",
                settings={ENABLED_KEY: True},
            )
        )
        members: list[m.OrgMember] = []
        for name in ("Ada", "Bob"):
            subject = f"user-{uuid.uuid4().hex[:8]}"
            member = m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=subject_uuid_for(subject),
                display_name=name,
            )
            db.add(member)
            members.append(member)
        await db.flush()
        personal = await get_or_create_personal_department(db, member=members[0])
        standard = m.Department(
            tenant_id=tenant, name="Sales", goal="", frame={}, presentation={"icon": "building"}
        )
        db.add(standard)
        await db.flush()
        return _World(tenant, members[0].id, members[1].id, personal.id, standard.id)


async def _dept(app_session: AppSessionFactory, w: _World, dept_id: uuid.UUID) -> m.Department:
    async with app_session(w.tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        db.expunge(dept)
        return dept


async def test_no_seat_can_be_granted_or_revoked_in_a_personal_department(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        h = _headers(w.tenant)
        for member in (w.other, w.owner):
            resp = await http.put(
                f"/api/v1/members/{member}/departments/{w.personal}",
                json={"seatRole": "dept_viewer"},
                headers=h,
            )
            assert resp.status_code == 409, resp.text
            assert "personal departments take no extra seats" in resp.text
        resp = await http.delete(f"/api/v1/members/{w.owner}/departments/{w.personal}", headers=h)
        assert resp.status_code == 409, resp.text
        # A standard department still takes seats.
        resp = await http.put(
            f"/api/v1/members/{w.other}/departments/{w.standard}",
            json={"seatRole": "dept_viewer"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text


async def test_grant_seat_service_refuses_anyone_but_the_owner(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        with pytest.raises(PersonalDepartmentSeatRefused):
            await grant_seat(
                db,
                tenant_id=w.tenant,
                member_id=w.other,
                department_id=w.personal,
                seat_role="dept_viewer",
                granted_by=None,
            )


async def test_the_owner_keeps_the_seat_created_with_the_department(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        seat, changed = await grant_seat(
            db,
            tenant_id=w.tenant,
            member_id=w.owner,
            department_id=w.personal,
            seat_role="dept_approver",
            granted_by=None,
        )
        assert seat.seat_role == "dept_approver"
        assert changed is False


async def test_a_personal_department_cannot_be_renamed(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    async with _http() as http:
        h = _headers(w.tenant)
        resp = await http.patch(f"/api/v1/departments/{w.personal}", json={"name": "X"}, headers=h)
        assert resp.status_code == 409, resp.text
        resp = await http.patch(
            f"/api/v1/departments/{w.personal}", json={"icon": "star"}, headers=h
        )
        assert resp.status_code == 200, resp.text
        resp = await http.patch(f"/api/v1/departments/{w.standard}", json={"name": "Y"}, headers=h)
        assert resp.status_code == 200, resp.text


async def test_editing_its_frame_or_budget_marks_it_overridden(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        h = _headers(w.tenant)
        for dept_id in (w.personal, w.standard):
            resp = await http.put(
                f"/api/v1/departments/{dept_id}/tools", json={"tools": {}}, headers=h
            )
            assert resp.status_code == 200, resp.text
            resp = await http.put(
                "/api/v1/budgets",
                json={"departmentId": str(dept_id), "hardLimitTokens": 5},
                headers=h,
            )
            assert resp.status_code == 200, resp.text

    personal = await _dept(app_session, w, w.personal)
    standard = await _dept(app_session, w, w.standard)
    assert personal.kind == DEPARTMENT_KIND_PERSONAL
    assert personal.frame_overridden_at is not None
    assert personal.budget_overridden_at is not None
    assert standard.frame_overridden_at is None
    assert standard.budget_overridden_at is None


async def test_resetting_a_tool_marks_the_frame_overridden(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    tools: dict[str, Any] = {"x": {"enabled": True}}
    async with app_session(w.tenant) as db:
        dept = await db.get(m.Department, w.personal)
        assert dept is not None
        dept.frame = {**(dept.frame or {}), "tools": tools}
    async with _http() as http:
        resp = await http.post(
            f"/api/v1/departments/{w.personal}/tools/x/reset", headers=_headers(w.tenant)
        )
        assert resp.status_code == 200, resp.text
    assert (await _dept(app_session, w, w.personal)).frame_overridden_at is not None


async def test_the_copilot_cannot_update_a_personal_department(
    app_session: AppSessionFactory,
) -> None:
    from oc8.copilot.capabilities import InvalidOperation, apply_operation

    w = await _world(app_session)
    for fields in ({"name": "X"}, {"icon": "star"}, {"goal": "g"}):
        async with app_session(w.tenant) as db:
            with pytest.raises(InvalidOperation) as caught:
                await apply_operation(
                    db,
                    tenant_id=w.tenant,
                    data={"type": "department.update", "departmentId": str(w.personal), **fields},
                )
        assert caught.value.reason == "a personal department cannot be edited"
    assert (await _dept(app_session, w, w.personal)).name == "Ada"
    async with app_session(w.tenant) as db:
        await apply_operation(
            db,
            tenant_id=w.tenant,
            data={"type": "department.update", "departmentId": str(w.standard), "name": "Z"},
        )
