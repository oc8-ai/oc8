"""Personal departments: no extra seats, no rename, and admin edits to the
frame or budget are stamped as overrides."""

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


async def test_the_copilot_cannot_delete_restore_or_hire_into_a_personal_department(
    app_session: AppSessionFactory,
) -> None:
    from oc8.copilot.capabilities import InvalidOperation, apply_operation

    w = await _world(app_session)
    live_ops: list[dict[str, Any]] = [
        {"type": "department.delete", "departmentId": str(w.personal)},
        {
            "type": "agent.create",
            "departmentId": str(w.personal),
            "name": "Helper",
            "roleTitle": "Helper",
            "mission": "help",
        },
    ]
    restore = {"type": "department.restore", "departmentId": str(w.personal)}

    async def refused(op: dict[str, Any]) -> None:
        async with app_session(w.tenant) as db:
            with pytest.raises(InvalidOperation) as caught:
                await apply_operation(db, tenant_id=w.tenant, data=op)
        assert caught.value.reason == "a personal department cannot be edited", op["type"]

    for op in live_ops:
        await refused(op)
    assert (await _dept(app_session, w, w.personal)).deleted_at is None

    # Restore only gets past the "is it archived" check for an archived one.
    async with app_session(w.tenant) as db:
        archived = await db.get(m.Department, w.personal)
        assert archived is not None
        archived.deleted_at = dt.datetime.now(dt.UTC)
    await refused(restore)
    assert (await _dept(app_session, w, w.personal)).deleted_at is not None


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


async def _agent_in(
    app_session: AppSessionFactory, w: _World, dept_id: uuid.UUID, *, archived: bool = False
) -> uuid.UUID:
    async with app_session(w.tenant) as db:
        agent = m.Agent(
            tenant_id=w.tenant,
            department_id=dept_id,
            name="Helper",
            mission="m0",
            status="stopped",
            definition={},
            deleted_at=dt.datetime.now(dt.UTC) if archived else None,
        )
        db.add(agent)
        await db.flush()
        return agent.id


def _agent_ops(agent_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    a = str(agent_id)
    return {
        "rename": {"type": "agent.rename", "agentId": a, "name": "Renamed"},
        "lifecycle": {"type": "agent.lifecycle.set", "agentId": a, "action": "start"},
        "delete": {"type": "agent.delete", "agentId": a},
        "mission": {"type": "agent.mission.set", "agentId": a, "mission": "new mission"},
        "narrowing": {"type": "agent.narrowing.set", "agentId": a, "narrowing": {}},
        "trigger": {
            "type": "trigger.create",
            "agentId": a,
            "kind": "cron",
            "taskText": "t",
            "cronExpression": "0 9 * * *",
        },
        "board": {"type": "board.create", "agentId": a, "title": "B", "spec": _SPEC},
    }


@pytest.mark.parametrize(
    "name", ["rename", "lifecycle", "delete", "mission", "narrowing", "trigger", "board"]
)
async def test_the_copilot_cannot_edit_a_personal_agent(
    app_session: AppSessionFactory, name: str
) -> None:
    from oc8.copilot.capabilities import InvalidOperation, apply_operation

    w = await _world(app_session)
    agent_id = await _agent_in(app_session, w, w.personal)
    async with app_session(w.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(db, tenant_id=w.tenant, data=_agent_ops(agent_id)[name])
    assert caught.value.reason == "a personal agent cannot be edited"
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert (agent.name, agent.mission, agent.status) == ("Helper", "m0", "stopped")
        assert agent.deleted_at is None
        boards = (await db.execute(select(m.DataBoard))).scalars().all()
        assert boards == []


async def test_the_copilot_cannot_restore_an_archived_personal_agent(
    app_session: AppSessionFactory,
) -> None:
    from oc8.copilot.capabilities import InvalidOperation, apply_operation

    w = await _world(app_session)
    agent_id = await _agent_in(app_session, w, w.personal, archived=True)
    async with app_session(w.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(
                db,
                tenant_id=w.tenant,
                data={"type": "agent.restore", "agentId": str(agent_id)},
            )
    assert caught.value.reason == "a personal agent cannot be edited"
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.deleted_at is not None


async def test_the_copilot_cannot_move_a_board_of_a_personal_agent(
    app_session: AppSessionFactory,
) -> None:
    from oc8.copilot.capabilities import InvalidOperation, apply_operation

    w = await _world(app_session)
    agent_id = await _agent_in(app_session, w, w.personal)
    async with app_session(w.tenant) as db:
        board = m.DataBoard(
            tenant_id=w.tenant,
            department_id=w.personal,
            agent_id=agent_id,
            title="B",
            description="",
            spec={},
        )
        db.add(board)
        await db.flush()
        board_id = board.id
    async with app_session(w.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(
                db,
                tenant_id=w.tenant,
                data={"type": "board.update", "boardId": str(board_id), "title": "T"},
            )
    assert caught.value.reason == "a personal agent cannot be edited"


async def test_the_copilot_still_edits_a_standard_agent(app_session: AppSessionFactory) -> None:
    from oc8.copilot.capabilities import apply_operation

    w = await _world(app_session)
    agent_id = await _agent_in(app_session, w, w.standard)
    async with app_session(w.tenant) as db:
        await apply_operation(
            db, tenant_id=w.tenant, data=_agent_ops(agent_id)["rename"], publish=False
        )
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.name == "Renamed"


async def test_target_revision_refuses_a_personal_agent(app_session: AppSessionFactory) -> None:
    from oc8.copilot.capabilities import InvalidOperation, parse_operations, target_revision

    w = await _world(app_session)
    personal = await _agent_in(app_session, w, w.personal)
    standard = await _agent_in(app_session, w, w.standard)
    async with app_session(w.tenant) as db:
        op = parse_operations([_agent_ops(personal)["rename"]])[0]
        with pytest.raises(InvalidOperation) as caught:
            await target_revision(db, op)
        assert caught.value.reason == "a personal agent cannot be edited"
        ok = parse_operations([_agent_ops(standard)["rename"]])[0]
        assert await target_revision(db, ok) is not None
