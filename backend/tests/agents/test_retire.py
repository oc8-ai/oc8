"""`oc8.agents.retire`: what deleting an agent stops, whichever path deletes it.

The REST routes have their own tests (`tests/api/test_agents_archive_and_list_query.py`,
`tests/api/test_departments_archive_and_list_query.py`); these pin the shared
step itself -- suspended runs, their approvals, Copilot follow-ups -- and the
paths that are not REST routes (the Copilot's operations, a rejected hire).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.hire import create_hire_request, resolve_hire_agent
from oc8.agents.retire import delete_or_archive_agent
from oc8.auth import Principal
from oc8.copilot.proposals import apply_proposal, create_proposal
from oc8.runtime.cancellation import AGENT_ARCHIVED, run_stop
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _agent(db: AsyncSession, tenant: uuid.UUID, *, with_run: bool = True) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", definition={})
    db.add(agent)
    await db.flush()
    if with_run:
        db.add(m.AgentRun(tenant_id=tenant, agent_id=agent.id, context={}, state="done"))
    await db.flush()
    return agent


def _cron(tenant: uuid.UUID, agent_id: uuid.UUID) -> m.Trigger:
    return m.Trigger(
        tenant_id=tenant,
        agent_id=agent_id,
        kind="cron",
        task_text="daily",
        cron_expression="0 9 * * *",
        enabled=True,
    )


def _follow_up(tenant: uuid.UUID, agent_id: uuid.UUID) -> m.Trigger:
    return m.Trigger(
        tenant_id=tenant,
        agent_id=agent_id,
        kind="once",
        task_text="Check the venue replies",
        next_run_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=1),
        chat_session_id=uuid.uuid4(),
        responsibility_id=uuid.uuid4(),
        timezone="Europe/Berlin",
        enabled=True,
    )


@pytest.mark.parametrize("with_run", [True, False], ids=["archive", "hard-delete"])
async def test_copilot_follow_ups_survive_retiring(
    app_session: AppSessionFactory, with_run: bool
) -> None:
    """A follow-up belongs to a member's conversation and is managed in the
    Copilot; deleting the agent it runs on leaves it alone."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, with_run=with_run)
        follow_up = _follow_up(tenant, agent.id)
        db.add(follow_up)
        await db.flush()
        follow_up_id = follow_up.id
        outcome = await delete_or_archive_agent(db, agent, principal=None)
        assert outcome == ("archived" if with_run else "deleted")

    async with app_session(tenant) as db:
        kept = await db.get(m.Trigger, follow_up_id)
        assert kept is not None
        assert kept.enabled is True


async def test_archiving_expires_pending_approvals_and_drains_suspended_runs(
    app_session: AppSessionFactory,
) -> None:
    """Nothing of a deleted agent waits on a person: its pending approval is
    expired, and its suspended run is re-queued so the executor ends it as
    `agent_archived` -- the executor stays the one place that ends a run."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        agent.status = "running"
        waiting = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, context={}, state="waiting_for_approval"
        )
        db.add(waiting)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="tool_send",
            title="Send the offer",
            status="pending",
        )
        db.add(approval)
        await db.flush()
        agent_id, run_id, approval_id = agent.id, waiting.id, approval.id
        await delete_or_archive_agent(db, agent, principal=None)

    async with app_session(tenant) as db:
        refreshed = await db.get(m.ApprovalRequest, approval_id)
        assert refreshed is not None
        assert refreshed.status == "expired"
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.QUEUED.value
        kind = (
            await db.execute(
                select(m.RunCancellation.cancellation_kind).where(
                    m.RunCancellation.run_id == run_id
                )
            )
        ).scalar_one()
        assert kind == AGENT_ARCHIVED

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False)
    )

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.INTERRUPTED.value
        assert run.context["cancellation_kind"] == AGENT_ARCHIVED
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert agent.status == "idle"


async def test_run_stop_stops_a_running_run_of_an_archived_agent(
    app_session: AppSessionFactory,
) -> None:
    """The per-step probe a container run and the MCP gateway ask: a run whose
    agent was archived stops, even without a cancel row (a run that started at
    the moment of deletion)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, context={}, state="running")
        db.add(run)
        await db.flush()
        agent.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.flush()
        stop = await run_stop(db, run)
        assert stop is not None
        assert stop.kind == AGENT_ARCHIVED
        assert stop.context_patch == {"cancellation_kind": AGENT_ARCHIVED}


async def test_copilot_agent_delete_retires_the_agent(
    app_session: AppSessionFactory, acme_tenant: uuid.UUID
) -> None:
    actor = Principal(subject="operator-1", tenant_id=acme_tenant, role="org_admin")
    async with app_session(acme_tenant) as db:
        agent = await _agent(db, acme_tenant)
        trigger = _cron(acme_tenant, agent.id)
        queued = m.AgentRun(tenant_id=acme_tenant, agent_id=agent.id, context={}, state="queued")
        db.add_all([trigger, queued])
        await db.flush()
        proposal = await create_proposal(
            db, actor, [{"type": "agent.delete", "agentId": str(agent.id)}]
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        trigger_id, run_id = trigger.id, queued.id

    async with app_session(acme_tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.enabled is False
        cancelled = (
            await db.execute(
                select(m.RunCancellation.run_id).where(m.RunCancellation.run_id == run_id)
            )
        ).scalar_one_or_none()
        assert cancelled == run_id


async def test_copilot_department_delete_retires_its_agents(
    app_session: AppSessionFactory, acme_tenant: uuid.UUID
) -> None:
    actor = Principal(subject="operator-1", tenant_id=acme_tenant, role="org_admin")
    async with app_session(acme_tenant) as db:
        veteran = await _agent(db, acme_tenant)
        fresh = m.Agent(
            tenant_id=acme_tenant, department_id=veteran.department_id, name="Fresh", definition={}
        )
        db.add(fresh)
        await db.flush()
        kept, orphan = _cron(acme_tenant, veteran.id), _cron(acme_tenant, fresh.id)
        db.add_all([kept, orphan])
        await db.flush()
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "department.delete", "departmentId": str(veteran.department_id)}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"
        kept_id, orphan_id = kept.id, orphan.id

    async with app_session(acme_tenant) as db:
        assert await db.get(m.Trigger, orphan_id) is None
        refreshed = await db.get(m.Trigger, kept_id)
        assert refreshed is not None
        assert refreshed.enabled is False


async def test_rejecting_a_hire_retires_the_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, with_run=False)
        agent.status = "pending_approval"
        trigger = _cron(tenant, agent.id)
        db.add(trigger)
        await db.flush()
        request = await create_hire_request(db, agent=agent)
        request.status = "rejected"
        await resolve_hire_agent(db, approval_request=request, decision="reject")
        trigger_id = trigger.id

    async with app_session(tenant) as db:
        refreshed = await db.get(m.Trigger, trigger_id)
        assert refreshed is not None
        assert refreshed.enabled is False
