"""Runs a person started indirectly -- a task on the board, the follow-up that
carries out a decision -- act for that person, like delegations and wake-ups."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import REQUEST_DECISION, execute_control_tool
from oc8.agent.decision_followup import carry_out_decision
from oc8.auth.principal import Principal
from oc8.authz.pdp import Decision, Effect
from oc8.authz.scope import HumanActor, scope_for_principal
from oc8.connections.acting import resolve_acting_person
from oc8.modelrouter import ToolCall
from oc8.runtime.queue import RunQueue
from oc8.workspace.tasks import create_task
from tests.conftest import AppSessionFactory
from tests.connections.helpers import member

pytestmark = pytest.mark.asyncio


@pytest.fixture
def queue(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: q)
    return q


async def test_a_board_task_acts_for_the_member_who_asked(
    app_session: AppSessionFactory, queue: Any
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Support", frame={})
        db.add(dept)
        await db.flush()
        lead = m.Agent(tenant_id=tenant, department_id=dept.id, name="Lead", is_team_lead=True)
        db.add(lead)
        await db.flush()
        dept.team_lead_agent_id = lead.id
        principal = Principal(subject="anna@acme.test", tenant_id=tenant, role="org_admin")
        anna, scope = await scope_for_principal(db, principal, upsert=True)
        assert anna is not None
        await create_task(
            db,
            actor=HumanActor(principal=principal, member=anna, scope=scope),
            tenant_id=tenant,
            department_id=dept.id,
            member_id=anna.id,
            instructions="Bitte die offenen Tickets zusammenfassen.",
        )
    async with app_session(tenant) as db:
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == lead.id))
        ).scalar_one()
        assert (await resolve_acting_person(db, run)).member_id == anna.id


@pytest.mark.parametrize("trigger", [False, True])
async def test_carrying_out_a_decision_acts_for_the_original_runs_person(
    app_session: AppSessionFactory, queue: Any, trigger: bool
) -> None:
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Sina")
        db.add(agent)
        await db.flush()
        context: dict[str, Any] = {"originating_operator": "anna@acme.test"}
        if trigger:
            row = m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron",
                task_text="Täglich",
                cron_expression="0 8 * * *",
                created_by_member_id=anna,
            )
            db.add(row)
            await db.flush()
            context = {"trigger_id": str(row.id)}
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="manual", context=context)
        task = m.Task(
            tenant_id=tenant,
            department_id=agent.department_id,
            assigned_agent_id=agent.id,
            title="Ticket",
            state="in_progress",
        )
        db.add_all([run, task])
        await db.flush()
        await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="1",
                name=REQUEST_DECISION.name,
                arguments={"question": "Erstatten?", "context": "Doppelte Abbuchung."},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
        approval = (await db.execute(select(m.ApprovalRequest))).scalar_one()
        followup_id = await carry_out_decision(
            db, approval=approval, decision="approve", reason="", option=None
        )
    assert followup_id is not None
    async with app_session(tenant) as db:
        followup = await db.get(m.AgentRun, followup_id)
        assert followup is not None
        assert (await resolve_acting_person(db, followup)).member_id == anna
        # A schedule's own conditions keep applying to what it set off.
        assert followup.context.get("trigger_id") == (context.get("trigger_id"))
