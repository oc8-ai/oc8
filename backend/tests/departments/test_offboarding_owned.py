"""Offboarding a member stops what their own departments run: agents, triggers,
tool bindings, and the in-flight runs and open approvals of those agents."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.authz.scope import subject_uuid_for
from oc8.copilot.profile import offboard_member
from oc8.departments.personal import (
    ENABLED_KEY,
    get_or_create_personal_department,
    stop_departments_owned_by,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _tenant_with_member(db: Any, tenant: uuid.UUID) -> m.OrgMember:
    db.add(
        m.Organization(
            id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={ENABLED_KEY: True}
        )
    )
    subject = f"user-{uuid.uuid4().hex[:8]}"
    member = m.OrgMember(
        tenant_id=tenant,
        subject=subject,
        subject_uuid=subject_uuid_for(subject),
        display_name="Ada",
    )
    db.add(member)
    await db.flush()
    return member


async def _agent_with_gear(
    db: Any, tenant: uuid.UUID, dept_id: uuid.UUID, name: str
) -> tuple[m.Agent, m.Trigger, m.AgentToolAccess]:
    agent = m.Agent(
        tenant_id=tenant, department_id=dept_id, name=name, definition={}, status="running"
    )
    db.add(agent)
    await db.flush()
    trigger = m.Trigger(
        tenant_id=tenant,
        agent_id=agent.id,
        kind="cron",
        task_text="daily",
        cron_expression="0 9 * * *",
        enabled=True,
        last_skip_reason="stale",
    )
    binding = m.AgentToolAccess(tenant_id=tenant, agent_id=agent.id, tool_key="mail", mode="user")
    db.add_all([trigger, binding])
    await db.flush()
    return agent, trigger, binding


async def test_offboarding_stops_agents_disables_triggers_and_revokes_bindings(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        owner = await _tenant_with_member(db, tenant)
        dept = await get_or_create_personal_department(db, member=owner)
        agent, trigger, binding = await _agent_with_gear(db, tenant, dept.id, "Mailer")
        shared = m.Department(tenant_id=tenant, name="Shared")
        db.add(shared)
        await db.flush()
        control, c_trigger, c_binding = await _agent_with_gear(db, tenant, shared.id, "Ctl")
        ids = (owner.id, dept.id, agent.id, trigger.id, binding.id)
        cids = (control.id, c_trigger.id, c_binding.id)

    async with app_session(tenant) as db:
        await offboard_member(db, tenant_id=tenant, member_id=ids[0])

    async with app_session(tenant) as db:
        a = await db.get(m.Agent, ids[2])
        assert a is not None and a.status == "stopped"
        t = await db.get(m.Trigger, ids[3])
        assert t is not None and t.enabled is False and t.last_skip_reason is None
        assert await db.get(m.AgentToolAccess, ids[4]) is None
        d = await db.get(m.Department, ids[1])
        assert d is not None and d.deleted_at is None
        c = await db.get(m.Agent, cids[0])
        ct = await db.get(m.Trigger, cids[1])
        assert c is not None and c.status == "running"
        assert ct is not None and ct.enabled is True and ct.last_skip_reason == "stale"
        assert await db.get(m.AgentToolAccess, cids[2]) is not None
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "department.owner_offboarded")
                )
            )
            .scalars()
            .all()
        )
        assert [e.resource for e in events] == [{"member_id": str(ids[0]), "agents_stopped": 1}]


async def test_offboarding_cancels_open_runs_and_expires_open_approvals(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        owner = await _tenant_with_member(db, tenant)
        dept = await get_or_create_personal_department(db, member=owner)
        agent, _, _ = await _agent_with_gear(db, tenant, dept.id, "Mailer")
        queued = m.AgentRun(tenant_id=tenant, agent_id=agent.id, context={}, state="queued")
        parked = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, context={}, state="waiting_for_approval"
        )
        done = m.AgentRun(tenant_id=tenant, agent_id=agent.id, context={}, state="done")
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="tool_send",
            title="Send it",
            status="pending",
        )
        db.add_all([queued, parked, done, approval])
        await db.flush()
        ids = (owner.id, queued.id, parked.id, done.id, approval.id)

    async with app_session(tenant) as db:
        await offboard_member(db, tenant_id=tenant, member_id=ids[0])

    async with app_session(tenant) as db:
        cancelled = {
            c.run_id for c in (await db.execute(select(m.RunCancellation))).scalars().all()
        }
        assert cancelled == {ids[1], ids[2]}
        ap = await db.get(m.ApprovalRequest, ids[4])
        assert ap is not None and ap.status == "expired"


async def test_the_hook_also_covers_an_archived_owned_department(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        owner = await _tenant_with_member(db, tenant)
        live = await get_or_create_personal_department(db, member=owner)
        old = m.Department(
            tenant_id=tenant,
            name="Old",
            kind=live.kind,
            owner_member_id=owner.id,
            frame=live.frame,
            deleted_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add(old)
        await db.flush()
        _, t1, _ = await _agent_with_gear(db, tenant, live.id, "A")
        _, t2, _ = await _agent_with_gear(db, tenant, old.id, "B")
        ids = (owner.id, t1.id, t2.id)

        stopped = await stop_departments_owned_by(db, tenant_id=tenant, member_id=owner.id)
        assert stopped == 2
        await db.refresh(t1)
        await db.refresh(t2)
        assert (t1.enabled, t2.enabled) == (False, False)
        assert ids[1] != ids[2]


async def test_a_returning_subject_gets_a_fresh_department(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        owner = await _tenant_with_member(db, tenant)
        dept = await get_or_create_personal_department(db, member=owner)
        agent, _, _ = await _agent_with_gear(db, tenant, dept.id, "Mailer")
        owner_id, old_dept, agent_id = owner.id, dept.id, agent.id
        subject, display = owner.subject, owner.display_name

    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, owner_id)
        assert member is not None
        await offboard_member(db, tenant_id=tenant, member_id=owner_id)
        member.deleted_at = dt.datetime.now(tz=dt.UTC)
        member.subject = f"{subject}#gone"
        await db.flush()
        returning = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=subject_uuid_for(subject),
            display_name=display,
        )
        db.add(returning)
        await db.flush()
        fresh = await get_or_create_personal_department(db, member=returning)
        assert fresh.id != old_dept
        old_agent = await db.get(m.Agent, agent_id)
        assert old_agent is not None and old_agent.status == "stopped"
