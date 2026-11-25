"""A4: a personal agent's questions, tasks and boards are its owner's alone.

The workspace queues (`open_clarifications`, `visible_tasks`), the department
task board and the data boards each took "unrestricted" or "tenant-wide
`agent:view`" to mean every department -- a personal one's included, with the
question the agent asked, the task text and the board's records. They now
leave out the departments private to the caller
(`departments.repo.private_department_ids`), the owner keeps them.

The world: `owner` owns a personal department with agent Pia, which parked a
question, holds a task and produced a board; a standard "Vertrieb" with agent
Nora has the same three. `admin` is an `org_admin`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER
from oc8.boards.sharing import board_audience
from oc8.copilot.profile import offboard_member
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SECRET_QUESTION = "which of my private mails should I answer first?"
_SECRET_TASK = "sort the owner's private inbox"
_SECRET_BOARD = "Owner's private numbers"

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


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    personal: uuid.UUID
    shared: uuid.UUID
    pia_question: uuid.UUID
    nora_question: uuid.UUID
    pia_task: uuid.UUID
    nora_task: uuid.UUID
    pia_board: uuid.UUID
    nora_board: uuid.UUID


def _subject_uuid(subject: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


def _owner(w: _World) -> dict[str, str]:
    return _headers(w.tenant, "owner", "member")


def _admin(w: _World) -> dict[str, str]:
    return _headers(w.tenant, "admin", "org_admin")


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _world(app_session: AppSessionFactory) -> _World:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={}))
        shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(shared)
        members: dict[str, m.OrgMember] = {}
        for subject in ("owner", "admin"):
            member = m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=_subject_uuid(subject),
                display_name=subject.title(),
            )
            db.add(member)
            members[subject] = member
        await db.flush()
        personal = m.Department(
            tenant_id=tenant,
            name="Owner",
            frame={"closed": True},
            kind="personal",
            owner_member_id=members["owner"].id,
        )
        db.add(personal)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=members["owner"].id,
                department_id=personal.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            )
        )
        nora = m.Agent(tenant_id=tenant, department_id=shared.id, name="Nora")
        pia = m.Agent(tenant_id=tenant, department_id=personal.id, name="Pia")
        db.add_all([nora, pia])
        await db.flush()
        pia_task = m.Task(
            tenant_id=tenant,
            department_id=personal.id,
            assigned_agent_id=pia.id,
            title=_SECRET_TASK,
            state="in_progress",
        )
        nora_task = m.Task(
            tenant_id=tenant,
            department_id=shared.id,
            assigned_agent_id=nora.id,
            title="call the customer",
            state="in_progress",
        )
        db.add_all([pia_task, nora_task])
        await db.flush()
        pia_run = m.AgentRun(
            tenant_id=tenant, agent_id=pia.id, task_id=pia_task.id, state="waiting_for_input"
        )
        nora_run = m.AgentRun(
            tenant_id=tenant, agent_id=nora.id, task_id=nora_task.id, state="waiting_for_input"
        )
        db.add_all([pia_run, nora_run])
        await db.flush()
        pia_question = m.Clarification(
            tenant_id=tenant, run_id=pia_run.id, agent_id=pia.id, question=_SECRET_QUESTION
        )
        nora_question = m.Clarification(
            tenant_id=tenant, run_id=nora_run.id, agent_id=nora.id, question="which customer?"
        )
        pia_board = m.DataBoard(
            tenant_id=tenant,
            department_id=personal.id,
            agent_id=pia.id,
            title=_SECRET_BOARD,
            spec=_SPEC,
        )
        nora_board = m.DataBoard(
            tenant_id=tenant, department_id=shared.id, agent_id=nora.id, title="Sales", spec=_SPEC
        )
        db.add_all([pia_question, nora_question, pia_board, nora_board])
        await db.flush()
        return _World(
            tenant=tenant,
            personal=personal.id,
            shared=shared.id,
            pia_question=pia_question.id,
            nora_question=nora_question.id,
            pia_task=pia_task.id,
            nora_task=nora_task.id,
            pia_board=pia_board.id,
            nora_board=nora_board.id,
        )


async def test_admin_does_not_see_a_personal_clarification(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get("/api/v1/clarifications", headers=_admin(w))
        assert admin.status_code == 200, admin.text
        ids = {row["id"] for row in admin.json()}
        assert str(w.pia_question) not in ids
        assert _SECRET_QUESTION not in admin.text
        assert str(w.nora_question) in ids

        owner = await http.get("/api/v1/clarifications", headers=_owner(w))
        assert owner.status_code == 200, owner.text
        assert {row["id"] for row in owner.json()} == {str(w.pia_question)}


async def test_admin_does_not_see_personal_tasks_or_the_department_board(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get("/api/v1/tasks", headers=_admin(w))
        assert admin.status_code == 200, admin.text
        ids = {row["id"] for row in admin.json()}
        assert str(w.pia_task) not in ids
        assert _SECRET_TASK not in admin.text
        assert str(w.nora_task) in ids

        narrowed = await http.get(
            "/api/v1/tasks", params={"department_id": str(w.personal)}, headers=_admin(w)
        )
        assert narrowed.status_code == 200, narrowed.text
        assert narrowed.json() == []

        board = await http.get(f"/api/v1/departments/{w.personal}/board", headers=_admin(w))
        assert board.status_code == 404, board.text
        assert _SECRET_TASK not in board.text
        # The control: the admin still reads a standard department's board.
        shared = await http.get(f"/api/v1/departments/{w.shared}/board", headers=_admin(w))
        assert shared.status_code == 200, shared.text

        owner = await http.get("/api/v1/tasks", headers=_owner(w))
        assert owner.status_code == 200, owner.text
        assert {row["id"] for row in owner.json()} == {str(w.pia_task)}
        owner_board = await http.get(f"/api/v1/departments/{w.personal}/board", headers=_owner(w))
        assert owner_board.status_code == 200, owner_board.text
        assert str(w.pia_task) in {t["id"] for t in owner_board.json()["tasks"]}


async def test_the_department_detail_says_whether_its_content_is_hidden(
    app_session: AppSessionFactory,
) -> None:
    """The department page asks `contentHidden` before it asks for the board:
    an admin reading a member's personal department gets True (the board
    would be a 404), the owner and every standard department False."""
    w = await _world(app_session)
    async with _http() as http:
        admin = await http.get(f"/api/v1/departments/{w.personal}", headers=_admin(w))
        assert admin.status_code == 200, admin.text
        assert admin.json()["contentHidden"] is True

        shared = await http.get(f"/api/v1/departments/{w.shared}", headers=_admin(w))
        assert shared.status_code == 200, shared.text
        assert shared.json()["contentHidden"] is False

        owner = await http.get(f"/api/v1/departments/{w.personal}", headers=_owner(w))
        assert owner.status_code == 200, owner.text
        assert owner.json()["contentHidden"] is False


async def test_a_departed_owners_department_stays_hidden_from_an_admin(
    app_session: AppSessionFactory,
) -> None:
    """Offboarding the owner does not hand their department's content to an
    admin: the detail still says `contentHidden`."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        owner = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "owner"))
        ).scalar_one()
        await offboard_member(db, tenant_id=w.tenant, member_id=owner.id)
        owner.deleted_at = dt.datetime.now(tz=dt.UTC)
        owner.subject = "owner#gone"
        await db.flush()
    async with _http() as http:
        admin = await http.get(f"/api/v1/departments/{w.personal}", headers=_admin(w))
        assert admin.status_code == 200, admin.text
        assert admin.json()["contentHidden"] is True


async def test_admin_does_not_see_a_personal_agents_board_records(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        listed = await http.get("/api/v1/boards", headers=_admin(w))
        assert listed.status_code == 200, listed.text
        ids = {row["id"] for row in listed.json()}
        assert str(w.pia_board) not in ids
        assert _SECRET_BOARD not in listed.text
        assert str(w.nora_board) in ids

        one = await http.get(f"/api/v1/boards/{w.pia_board}", headers=_admin(w))
        assert one.status_code == 404, one.text
        query = await http.post(
            f"/api/v1/boards/{w.pia_board}/query", json=_QUERY, headers=_admin(w)
        )
        assert query.status_code == 404, query.text

        owner = await http.get(f"/api/v1/boards/{w.pia_board}", headers=_owner(w))
        assert owner.status_code == 200, owner.text
        owner_query = await http.post(
            f"/api/v1/boards/{w.pia_board}/query", json=_QUERY, headers=_owner(w)
        )
        assert owner_query.status_code == 200, owner_query.text


async def test_a_personal_board_is_not_announced_to_all_departments(
    app_session: AppSessionFactory,
) -> None:
    """`board_audience` decides who a share newly tells: an `all_departments`
    member does not see a personal board, so they are not counted in it."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        everywhere = m.OrgMember(
            tenant_id=w.tenant,
            subject="everywhere",
            subject_uuid=_subject_uuid("everywhere"),
            display_name="Everywhere",
            all_departments=True,
        )
        db.add(everywhere)
        await db.flush()
        boards = {
            b.id: b
            for b in (
                await db.execute(select(m.DataBoard).where(m.DataBoard.tenant_id == w.tenant))
            ).scalars()
        }
        assert everywhere.id not in await board_audience(db, boards[w.pia_board])
        assert everywhere.id in await board_audience(db, boards[w.nora_board])
