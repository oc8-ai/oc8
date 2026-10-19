"""The task-board half of the workspace queue, at the level the endpoint sits
on -- same reason `test_queue.py` drives `workspace.queue` directly: every way
this module can be subtly wrong is invisible through a route that returns 200
either way.

* `visible_tasks` failing open for an empty scope (the three-way branch this
  whole family of modules exists to make unwritable),
* `department_id` widening a scope instead of intersecting it,
* `create_task` handing the run to anybody but the department's team lead, or
  opening a SECOND task when the run starts because `task_id` was not passed
  to `enqueue_run`,
* a department with no team lead being silently accepted, which here -- unlike
  `route_to_team_lead` -- is wrong: there is no human left to leave it for.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.auth.principal import Principal
from oc8.authz.scope import HumanActor, scope_for_principal
from oc8.runtime.queue import RunQueue
from oc8.tenants.activity import TenantInactive, set_tenant_activity_gate
from oc8.workspace.tasks import NoTeamLead, create_task, visible_tasks
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture
def queue(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: q)
    return q


async def _actor(db: Any, tenant: uuid.UUID, subject: str, role: str = "member") -> HumanActor:
    principal = Principal(subject=subject, tenant_id=tenant, role=role)
    member, scope = await scope_for_principal(db, principal, upsert=True)
    assert member is not None
    return HumanActor(principal=principal, member=member, scope=scope)


async def _seat(
    db: Any, tenant: uuid.UUID, subject: str, department_id: uuid.UUID, seat_role: str
) -> None:
    member: m.OrgMember | None = (
        await db.execute(
            select(m.OrgMember).where(
                m.OrgMember.subject == subject, m.OrgMember.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if member is None:
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}"),
            display_name=subject,
        )
        db.add(member)
        await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=member.id,
            department_id=department_id,
            seat_role=seat_role,
        )
    )
    await db.flush()


async def _department(db: Any, tenant: uuid.UUID, name: str, *, with_lead: bool) -> m.Department:
    dept = m.Department(tenant_id=tenant, name=name, frame={})
    db.add(dept)
    await db.flush()
    if with_lead:
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name=f"{name}-Lead",
            status="idle",
            is_team_lead=True,
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(lead)
        await db.flush()
        dept.team_lead_agent_id = lead.id
        await db.flush()
    return dept


async def _task(
    db: Any, tenant: uuid.UUID, department_id: uuid.UUID, *, title: str, state: str = "backlog"
) -> m.Task:
    task = m.Task(tenant_id=tenant, department_id=department_id, title=title, state=state)
    db.add(task)
    await db.flush()
    return task


# ---------------------------------------------------------------------- reading


async def test_the_board_holds_only_this_seats_departments(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = await _department(db, tenant, "Vertrieb", with_lead=False)
        engineering = await _department(db, tenant, "Entwicklung", with_lead=False)
        sales_task = await _task(db, tenant, sales.id, title="Angebot nachfassen")
        await _task(db, tenant, engineering.id, title="Migration planen")
        await _seat(db, tenant, "hos", sales.id, "dept_viewer")

        rows = await visible_tasks(db, actor=await _actor(db, tenant, "hos"))

    assert [r.id for r in rows] == [sales_task.id]
    assert rows[0].department_name == "Vertrieb"
    assert rows[0].column == "backlog"


async def test_an_empty_scope_reads_nothing_rather_than_everything(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = await _department(db, tenant, "Vertrieb", with_lead=False)
        await _task(db, tenant, dept.id, title="Angebot nachfassen")
        actor = await _actor(db, tenant, "nobody")
        assert actor.scope.is_empty is True
        assert await visible_tasks(db, actor=actor) == []


async def test_the_unrestricted_see_every_department(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = await _department(db, tenant, "Vertrieb", with_lead=False)
        engineering = await _department(db, tenant, "Entwicklung", with_lead=False)
        sales_task = await _task(db, tenant, sales.id, title="Angebot nachfassen")
        eng_task = await _task(db, tenant, engineering.id, title="Migration planen")

        actor = await _actor(db, tenant, "boss", role="org_admin")
        rows = await visible_tasks(db, actor=actor)

    assert {r.id for r in rows} == {sales_task.id, eng_task.id}


async def test_a_department_outside_the_scope_intersects_rather_than_widens(
    app_session: AppSessionFactory,
) -> None:
    """Asking for a department the actor cannot see returns an empty board, not
    a 403 that would confirm the department exists."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = await _department(db, tenant, "Vertrieb", with_lead=False)
        engineering = await _department(db, tenant, "Entwicklung", with_lead=False)
        await _task(db, tenant, engineering.id, title="Migration planen")
        await _seat(db, tenant, "hos", sales.id, "dept_viewer")

        rows = await visible_tasks(
            db, actor=await _actor(db, tenant, "hos"), department_id=engineering.id
        )

    assert rows == []


# --------------------------------------------------------------------- creating


async def test_creating_a_task_starts_a_run_on_the_team_lead(
    app_session: AppSessionFactory, queue: Any
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = await _department(db, tenant, "Kundenservice", with_lead=True)
        await _seat(db, tenant, "justin", dept.id, "dept_viewer")
        actor = await _actor(db, tenant, "justin")

        row = await create_task(
            db,
            actor=actor,
            tenant_id=tenant,
            department_id=dept.id,
            member_id=actor.member.id,
            instructions="Bitte die offenen Tickets aus der letzten Woche zusammenfassen.",
        )
        assert row.department_id == dept.id
        assert row.agent_id == dept.team_lead_agent_id
        assert row.requested_by_member_id == actor.member.id

    async with app_session(tenant) as db:
        task = await db.get(m.Task, row.id)
        assert task is not None
        assert task.assigned_agent_id == dept.team_lead_agent_id
        assert task.payload["requested_by_member_id"] == str(actor.member.id)

        run = (
            (
                await db.execute(
                    select(m.AgentRun).where(m.AgentRun.agent_id == task.assigned_agent_id)
                )
            )
            .scalars()
            .first()
        )
        assert run is not None, "a task nobody runs is a row, not work"
        assert run.source == "manual"
        assert run.task_id == task.id, "the run works the task this call created, not a second one"


async def test_a_department_without_a_team_lead_refuses_rather_than_silently_drops(
    app_session: AppSessionFactory, queue: Any
) -> None:
    """Unlike `route_to_team_lead`, there is no human on the other end of this
    call to leave the work for -- the member asking IS the human, and must be
    told now rather than have the request vanish."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = await _department(db, tenant, "Kundenservice", with_lead=False)
        await _seat(db, tenant, "justin", dept.id, "dept_viewer")
        actor = await _actor(db, tenant, "justin")

        with pytest.raises(NoTeamLead):
            await create_task(
                db,
                actor=actor,
                tenant_id=tenant,
                department_id=dept.id,
                member_id=actor.member.id,
                instructions="Irgendwas erledigen.",
            )


async def test_cannot_create_a_task_in_a_department_outside_the_scope(
    app_session: AppSessionFactory, queue: Any
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = await _department(db, tenant, "Vertrieb", with_lead=False)
        engineering = await _department(db, tenant, "Entwicklung", with_lead=True)
        await _seat(db, tenant, "justin", sales.id, "dept_viewer")
        actor = await _actor(db, tenant, "justin")

        with pytest.raises(NoTeamLead):
            await create_task(
                db,
                actor=actor,
                tenant_id=tenant,
                department_id=engineering.id,
                member_id=actor.member.id,
                instructions="Irgendwas erledigen.",
            )


def _gate_saying_no_for(tenant: uuid.UUID):  # type: ignore[no-untyped-def]
    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    return gate


async def test_a_task_for_an_inactive_organisation_is_refused_and_not_written(
    app_session: AppSessionFactory, queue: Any
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = await _department(db, tenant, "Kundenservice", with_lead=True)
        await _seat(db, tenant, "justin", dept.id, "dept_viewer")
        dept_id = dept.id
        lead_id = dept.team_lead_agent_id
    set_tenant_activity_gate(_gate_saying_no_for(tenant))
    async with app_session(tenant) as db:
        actor = await _actor(db, tenant, "justin")
        with pytest.raises(TenantInactive):
            await create_task(
                db,
                actor=actor,
                tenant_id=tenant,
                department_id=dept_id,
                member_id=actor.member.id,
                instructions="Bitte die offenen Tickets zusammenfassen.",
            )
        tasks = (
            (await db.execute(select(m.Task).where(m.Task.department_id == dept_id)))
            .scalars()
            .all()
        )
        assert tasks == [], "refused before the task is built"
    async with app_session(tenant) as db:
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == lead_id)))
            .scalars()
            .all()
        )
        assert runs == []
