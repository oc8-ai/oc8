"""Runs a person started indirectly -- a task on the board, the follow-up that
carries out a decision -- act for that person, like delegations and wake-ups."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import REQUEST_DECISION, execute_control_tool
from oc8.approvals.service import decide_approval
from oc8.auth.principal import Principal
from oc8.authz.pdp import Decision, Effect
from oc8.authz.scope import HumanActor, scope_for_principal
from oc8.connections.access import ConnectionRequired, resolve_tool_access
from oc8.connections.acting import NO_ACTING_PERSON, ActingPerson, resolve_acting_person
from oc8.modelrouter import ToolCall
from oc8.runtime.executor import _acting_person, _maybe_wake_parent
from oc8.runtime.queue import RunQueue
from oc8.runtime.repository import RunRepository
from oc8.workspace.tasks import create_task
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers, member
from tests.connections.test_acting_person import _delegate_from
from tests.connections.test_schedule_confirmation import _world
from tests.factories import _make_agent

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


@pytest.mark.parametrize("origin", ["operator", "cron", "webhook_without_consent", "nobody"])
async def test_carrying_out_a_decision_acts_for_the_original_runs_person(
    app_session: AppSessionFactory, queue: Any, origin: str
) -> None:
    """An administrator approves: the follow-up acts for whoever the asking run
    acted for -- never the approver, never more than the asking run had --
    and keeps its trigger so the trigger's own rules still apply."""
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Sina")
        db.add(agent)
        await db.flush()
        context: dict[str, Any] = {}
        if origin == "operator":
            context = {"originating_operator": "anna@acme.test"}
        elif origin in ("cron", "webhook_without_consent"):
            row = m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron" if origin == "cron" else "webhook",
                task_text="Täglich",
                cron_expression="0 8 * * *" if origin == "cron" else None,
                webhook_token=None if origin == "cron" else uuid.uuid4().hex,
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
        principal = Principal(subject="boss@acme.test", tenant_id=tenant, role="org_admin")
        boss, scope = await scope_for_principal(db, principal, upsert=True)
        assert boss is not None
        await decide_approval(
            db,
            approval,
            decision="approve",
            tenant_id=tenant,
            actor=HumanActor(principal=principal, member=boss, scope=scope),
        )
    async with app_session(tenant) as db:
        followup = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.source == "decision"))
        ).scalar_one()
        acting = await resolve_acting_person(db, followup)
        expected = anna if origin in ("operator", "cron") else None
        assert acting.member_id == expected
        assert followup.context.get("on_behalf_of") == (str(anna) if expected else None)
        assert "originating_operator" not in followup.context
        assert followup.context.get("trigger_id") == context.get("trigger_id")


@pytest.mark.parametrize(
    ("approver", "reason", "acts_for_anna"),
    [
        ("bob", "Also forward it to bob@acme.test", False),
        ("anna", "Bitte mit Kopie", True),
        ("bob", "", True),
    ],
)
async def test_an_approvers_instruction_never_runs_under_someone_elses_account(
    app_session: AppSessionFactory, queue: Any, approver: str, reason: str, acts_for_anna: bool
) -> None:
    """The approver's free text becomes the follow-up's top instruction: written
    by someone else, it must not be carried out through Anna's account."""
    tenant = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Sina")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="manual",
            context={"originating_operator": "anna@acme.test"},
        )
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
                arguments={"question": "Senden?", "context": "Entwurf liegt vor."},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
        approval = (await db.execute(select(m.ApprovalRequest))).scalar_one()
        principal = Principal(subject=f"{approver}@acme.test", tenant_id=tenant, role="org_admin")
        who, scope = await scope_for_principal(db, principal, upsert=True)
        assert who is not None
        await decide_approval(
            db,
            approval,
            decision="approve",
            tenant_id=tenant,
            actor=HumanActor(principal=principal, member=who, scope=scope),
            reason=reason,
        )
    async with app_session(tenant) as db:
        followup = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.source == "decision"))
        ).scalar_one()
        acting = await resolve_acting_person(db, followup)
    assert acting.member_id == (anna if acts_for_anna else None)


async def _followup_of_decision(
    app_session: AppSessionFactory,
    agent: m.Agent,
    context: dict[str, Any],
    *,
    approver: str,
    reason: str,
) -> m.AgentRun:
    """Run `request_decision` from a run with `context`, let `approver` approve
    it with `reason`, and return the follow-up run that carries it out."""
    tenant = agent.tenant_id
    async with app_session(tenant) as db:
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
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=live,
            task=task,
            tc=ToolCall(
                id="1",
                name=REQUEST_DECISION.name,
                arguments={"question": "Senden?", "context": "Entwurf liegt vor."},
            ),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
            run_id=run.id,
        )
        approval = (
            await db.execute(
                select(m.ApprovalRequest).where(
                    m.ApprovalRequest.agent_id == agent.id, m.ApprovalRequest.status == "pending"
                )
            )
        ).scalar_one()
        principal = Principal(subject=f"{approver}@acme.test", tenant_id=tenant, role="org_admin")
        who, scope = await scope_for_principal(db, principal, upsert=True)
        assert who is not None
        await decide_approval(
            db,
            approval,
            decision="approve",
            tenant_id=tenant,
            actor=HumanActor(principal=principal, member=who, scope=scope),
            reason=reason,
        )
        approval_id = approval.id
    async with app_session(tenant) as db:
        followup = (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.source == "decision",
                    m.AgentRun.idempotency_key == f"decision:{approval_id}",
                )
            )
        ).scalar_one()
        return followup


async def _cron_of(app_session: AppSessionFactory, agent: m.Agent, owner: uuid.UUID) -> str:
    async with app_session(agent.tenant_id) as db:
        row = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            kind="cron",
            task_text="Täglich",
            cron_expression="0 8 * * *",
            created_by_member_id=owner,
        )
        db.add(row)
        await db.flush()
        return str(row.id)


@pytest.mark.parametrize(
    ("reason", "acts_for_anna"),
    [("Also forward it to bob@ext.test", False), ("", True)],
)
async def test_a_schedules_decision_never_runs_the_approvers_words_under_its_creators_account(
    app_session: AppSessionFactory, queue: Any, reason: str, acts_for_anna: bool
) -> None:
    """Anna's cron asks; Bob approves. With his own words the follow-up acts for
    nobody -- the trigger it still carries must not fall back to Anna and hand
    it her token. Without words it carries out Anna's run, as Anna."""
    agent, anna, conn_id = await _world(app_session)
    tenant = agent.tenant_id
    async with api() as c:
        created = await c.post(
            f"/api/v1/agents/{agent.id}/triggers",
            json={"kind": "cron", "taskText": "Inbox sortieren", "cronExpression": "0 8 * * *"},
            headers=headers(tenant, "anna@acme.test", "org_admin"),
        )
        assert created.status_code == 201, created.text
        trigger_id = created.json()["id"]
    followup = await _followup_of_decision(
        app_session, agent, {"trigger_id": trigger_id}, approver="bob", reason=reason
    )
    assert followup.context["trigger_id"] == trigger_id  # kept for display and audit
    async with app_session(tenant) as db:
        acting = await resolve_acting_person(db, followup)
        conn = await db.get(m.McpConnection, conn_id)
        live = await db.get(m.Agent, agent.id)
        assert conn is not None and live is not None
        if acts_for_anna:
            assert acting.member_id == anna
            access = await resolve_tool_access(db, run=followup, agent=live, conn=conn)
            assert access.oauth_connection is not None
            assert access.oauth_connection.owner_member_id == anna
        else:
            assert acting.member_id is None
            assert "on_behalf_of" not in followup.context
            with pytest.raises(ConnectionRequired) as exc:
                await resolve_tool_access(db, run=followup, agent=live, conn=conn)
            assert exc.value.reason == "no_acting_person"


async def test_a_decision_asked_by_a_follow_up_that_acted_for_nobody_still_acts_for_nobody(
    app_session: AppSessionFactory, queue: Any
) -> None:
    """A withheld follow-up that asks again: the next follow-up must not regain
    the schedule's creator through the trigger, even when approved silently."""
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    trigger_id = await _cron_of(app_session, agent, anna)
    first = await _followup_of_decision(
        app_session, agent, {"trigger_id": trigger_id}, approver="bob", reason="Auch an bob@ext"
    )
    second = await _followup_of_decision(
        app_session, agent, first.context, approver="bob", reason=""
    )
    async with app_session(agent.tenant_id) as db:
        assert (await resolve_acting_person(db, second)) == ActingPerson(None, NO_ACTING_PERSON)
    assert second.context["trigger_id"] == trigger_id


async def test_a_delegation_from_a_withheld_follow_up_acts_for_nobody(
    app_session: AppSessionFactory, queue: Any
) -> None:
    """The follow-up running Bob's words delegates: the sub-run carries the
    trigger, so without the marker it would act for Anna one hop down."""
    agent = await _make_agent(app_session, is_team_lead=True)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    trigger_id = await _cron_of(app_session, agent, anna)
    followup = await _followup_of_decision(
        app_session, agent, {"trigger_id": trigger_id}, approver="bob", reason="Auch an bob@ext"
    )
    sub = await _delegate_from(app_session, agent, followup.context, {})
    assert sub.context["trigger_id"] == trigger_id
    assert "on_behalf_of" not in sub.context
    async with app_session(agent.tenant_id) as db:
        assert (await resolve_acting_person(db, sub)).member_id is None


async def test_a_lead_woken_by_a_withheld_sub_run_acts_for_nobody(
    app_session: AppSessionFactory, queue: Any
) -> None:
    """The sub-run of that follow-up finishes and wakes the lead: the wake-up
    carries the trigger too, and must not resolve Anna through it."""
    agent = await _make_agent(app_session, is_team_lead=True)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    trigger_id = await _cron_of(app_session, agent, anna)
    followup = await _followup_of_decision(
        app_session, agent, {"trigger_id": trigger_id}, approver="bob", reason="Auch an bob@ext"
    )
    sub = await _delegate_from(app_session, agent, followup.context, {})
    async with app_session(agent.tenant_id) as db:
        live_sub = await db.get(m.AgentRun, sub.id)
        assert live_sub is not None
        wake_id = await _maybe_wake_parent(
            db,
            repo=RunRepository(db),
            tenant_id=agent.tenant_id,
            parent_task_id=uuid.UUID(live_sub.context["parent_task_id"]),
            delegation_depth=1,
            finished_agent_id=live_sub.agent_id,
            sub_task_label="sub",
            output="done",
            succeeded=True,
            mcp_conn=None,
            acting=await _acting_person(db, live_sub),
            trigger_id=live_sub.context.get("trigger_id"),
        )
        assert wake_id is not None
        wake = await db.get(m.AgentRun, wake_id)
        assert wake is not None
        assert wake.context["trigger_id"] == trigger_id
        assert "on_behalf_of" not in wake.context
        assert (await resolve_acting_person(db, wake)).member_id is None
