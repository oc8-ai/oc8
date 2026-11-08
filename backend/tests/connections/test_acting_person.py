from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.connections.acting import (
    NO_ACTING_PERSON,
    SCHEDULE_WITHOUT_OWNER,
    ActingPerson,
    resolve_acting_person,
)
from oc8.runtime.executor import _maybe_wake_parent
from oc8.runtime.queue import RunQueue
from oc8.runtime.repository import RunRepository
from oc8.triggers.scheduler import fire_trigger
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers, member
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio


async def _run(
    app_session: AppSessionFactory, agent: m.Agent, context: dict[str, object]
) -> m.AgentRun:
    async with app_session(agent.tenant_id) as db:
        run = m.AgentRun(
            tenant_id=agent.tenant_id, agent_id=agent.id, context=context, source="manual"
        )
        db.add(run)
        await db.flush()
        return run


async def _resolve(app_session: AppSessionFactory, run: m.AgentRun) -> ActingPerson:
    async with app_session(run.tenant_id) as db:
        return await resolve_acting_person(db, await db.get(m.AgentRun, run.id))  # type: ignore[arg-type]


async def test_the_chat_member_acts(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    async with app_session(agent.tenant_id) as db:
        session = m.ChatSession(tenant_id=agent.tenant_id, agent_id=agent.id, member_id=anna)
        db.add(session)
        await db.flush()
        session_id = session.id
    run = await _run(app_session, agent, {"chat_session_id": str(session_id)})
    assert await _resolve(app_session, run) == ActingPerson(anna)


async def test_the_starter_of_a_manual_run_acts(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    run = await _run(app_session, agent, {"originating_operator": "anna@acme.test"})
    assert await _resolve(app_session, run) == ActingPerson(anna)


async def test_a_delegated_run_keeps_its_person(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    run = await _run(app_session, agent, {"on_behalf_of": str(anna)})
    assert await _resolve(app_session, run) == ActingPerson(anna)


async def test_a_trigger_acts_for_its_creator(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    async with app_session(agent.tenant_id) as db:
        trigger = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            kind="cron",
            task_text="t",
            cron_expression="0 8 * * *",
            created_by_member_id=anna,
        )
        db.add(trigger)
        await db.flush()
        trigger_id = trigger.id
    run = await _run(app_session, agent, {"trigger_id": str(trigger_id)})
    assert await _resolve(app_session, run) == ActingPerson(anna)


async def test_legacy_trigger_has_no_acting_person(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        trigger = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            kind="cron",
            task_text="t",
            cron_expression="0 8 * * *",
        )
        db.add(trigger)
        await db.flush()
        trigger_id = trigger.id
    run = await _run(app_session, agent, {"trigger_id": str(trigger_id)})
    assert await _resolve(app_session, run) == ActingPerson(None, SCHEDULE_WITHOUT_OWNER)


async def test_a_removed_member_acts_for_nobody(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    async with app_session(agent.tenant_id) as db:
        row = await db.get(m.OrgMember, anna)
        assert row is not None
        row.deleted_at = datetime.now(tz=UTC)
    run = await _run(app_session, agent, {"on_behalf_of": str(anna)})
    assert await _resolve(app_session, run) == ActingPerson(None, NO_ACTING_PERSON)
    nobody = await _run(app_session, agent, {"task": "x"})
    assert await _resolve(app_session, nobody) == ActingPerson(None, NO_ACTING_PERSON)


async def test_creating_or_resaving_a_trigger_records_its_owner(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        legacy = m.Trigger(
            tenant_id=tenant,
            agent_id=agent.id,
            kind="cron",
            task_text="t",
            cron_expression="0 8 * * *",
        )
        db.add(legacy)
        await db.flush()
        legacy_id = legacy.id
    admin = headers(tenant, "anna@acme.test", "org_admin")
    async with api() as c:
        created = await c.post(
            f"/api/v1/agents/{agent.id}/triggers",
            json={"kind": "cron", "taskText": "daily", "cronExpression": "0 9 * * *"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        assert created.json()["createdByMemberId"] == str(anna)
        resaved = await c.patch(
            f"/api/v1/triggers/{legacy_id}", json={"taskText": "t2"}, headers=admin
        )
        assert resaved.status_code == 200, resaved.text
        assert resaved.json()["createdByMemberId"] == str(anna)


async def test_a_trigger_fire_names_its_trigger(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await _make_agent(app_session)
    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    try:
        async with app_session(agent.tenant_id) as db:
            trigger = m.Trigger(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                kind="webhook",
                task_text="t",
                webhook_token=uuid.uuid4().hex,
            )
            db.add(trigger)
            await db.flush()
            trigger_id = trigger.id
            await fire_trigger(db, trigger, tenant_id=agent.tenant_id, payload={"a": 1})
    finally:
        await queue.close()
    async with app_session(agent.tenant_id) as db:
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent.id))
        ).scalar_one()
        assert run.context["trigger_id"] == str(trigger_id)


async def test_a_lead_wakes_up_for_the_same_person(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    anna = await member(app_session, tenant, "anna@acme.test")
    async with app_session(tenant) as db:
        lead = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lead")
        sub = m.Agent(tenant_id=tenant, department_id=dept_id, name="Ada")
        db.add_all([lead, sub])
        await db.flush()
        lead_task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(lead_task)
        await db.flush()
        wake_id = await _maybe_wake_parent(
            db,
            repo=RunRepository(db),
            tenant_id=tenant,
            parent_task_id=lead_task.id,
            delegation_depth=1,
            finished_agent_id=sub.id,
            sub_task_label="sub",
            output="done",
            succeeded=True,
            mcp_conn=None,
            on_behalf_of=anna,
            trigger_id="t-1",
        )
        assert wake_id is not None
        wake = await db.get(m.AgentRun, wake_id)
        assert wake is not None
        assert (wake.context["on_behalf_of"], wake.context["trigger_id"]) == (str(anna), "t-1")


async def test_a_webhook_payload_cannot_name_the_person(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller's JSON body only ever lands in the task text, never in the
    run context the resolver reads."""
    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    try:
        async with app_session(agent.tenant_id) as db:
            trigger = m.Trigger(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                kind="webhook",
                task_text="t",
                webhook_token=uuid.uuid4().hex,
            )
            db.add(trigger)
            await db.flush()
            await fire_trigger(
                db,
                trigger,
                tenant_id=agent.tenant_id,
                payload={
                    "on_behalf_of": str(anna),
                    "originating_operator": "anna@acme.test",
                    "trigger_id": str(uuid.uuid4()),
                },
            )
    finally:
        await queue.close()
    async with app_session(agent.tenant_id) as db:
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent.id))
        ).scalar_one()
        assert "on_behalf_of" not in run.context
        assert run.context["trigger_id"] == str(trigger.id)
        assert await resolve_acting_person(db, run) == ActingPerson(None, SCHEDULE_WITHOUT_OWNER)


async def test_a_chat_caller_cannot_inject_the_person(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.chat.service import send_message

    agent = await _make_agent(app_session)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    bob = await member(app_session, agent.tenant_id, "bob@acme.test")
    queue = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: queue)
    try:
        async with app_session(agent.tenant_id) as db:
            session = m.ChatSession(tenant_id=agent.tenant_id, agent_id=agent.id, member_id=anna)
            db.add(session)
            await db.flush()
            await send_message(
                db,
                session=session,
                tenant_id=agent.tenant_id,
                message="hi",
                originating_operator="anna@acme.test",
                extra_context={"on_behalf_of": str(bob), "trigger_id": str(uuid.uuid4())},
            )
    finally:
        await queue.close()
    async with app_session(agent.tenant_id) as db:
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent.id))
        ).scalar_one()
        assert "on_behalf_of" not in run.context and "trigger_id" not in run.context
        assert await resolve_acting_person(db, run) == ActingPerson(anna)


async def _delegate_from(
    app_session: AppSessionFactory,
    agent: m.Agent,
    context: dict[str, object],
    arguments: dict[str, object],
) -> m.AgentRun:
    from oc8.agent.control_tools import _delegate
    from oc8.modelrouter import ToolCall

    async with app_session(agent.tenant_id) as db:
        target = m.Agent(tenant_id=agent.tenant_id, department_id=agent.department_id, name="Sub")
        db.add(target)
        task = m.Task(
            tenant_id=agent.tenant_id,
            department_id=agent.department_id,
            assigned_agent_id=agent.id,
            title="job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(task)
        executing = m.AgentRun(
            tenant_id=agent.tenant_id, agent_id=agent.id, context=context, source="manual"
        )
        db.add(executing)
        await db.flush()
        _msg, sub_id = await _delegate(
            db,
            tenant_id=agent.tenant_id,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c",
                name="delegate_task",
                arguments={"agent_id": str(target.id), "task_text": "do", **arguments},
            ),
            mcp_conn=None,
            run_id=executing.id,
        )
        sub = await db.get(m.AgentRun, sub_id)
        assert sub is not None
        return sub


async def test_a_delegated_run_inherits_the_person_and_tool_input_cannot_change_it(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session, is_team_lead=True)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    bob = await member(app_session, agent.tenant_id, "bob@acme.test")
    sub = await _delegate_from(
        app_session,
        agent,
        {"originating_operator": "anna@acme.test"},
        {"on_behalf_of": str(bob), "originating_operator": "bob@acme.test"},
    )
    assert sub.context["on_behalf_of"] == str(anna)
    assert sub.context["originating_operator"] == "anna@acme.test"
    async with app_session(agent.tenant_id) as db:
        assert await resolve_acting_person(db, sub) == ActingPerson(anna)


async def test_a_second_hop_cannot_escalate_to_another_person(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session, is_team_lead=True)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    bob = await member(app_session, agent.tenant_id, "bob@acme.test")
    sub = await _delegate_from(
        app_session,
        agent,
        {"on_behalf_of": str(anna), "originating_operator": "bob@acme.test"},
        {},
    )
    assert sub.context["on_behalf_of"] == str(anna)
    async with app_session(agent.tenant_id) as db:
        assert await resolve_acting_person(db, sub) == ActingPerson(anna)
        assert bob != anna


async def test_a_delegated_scheduled_run_keeps_the_trigger_creator(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session, is_team_lead=True)
    anna = await member(app_session, agent.tenant_id, "anna@acme.test")
    async with app_session(agent.tenant_id) as db:
        trigger = m.Trigger(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            kind="cron",
            task_text="t",
            cron_expression="0 8 * * *",
            created_by_member_id=anna,
        )
        db.add(trigger)
        await db.flush()
        trigger_id = trigger.id
    sub = await _delegate_from(app_session, agent, {"trigger_id": str(trigger_id)}, {})
    assert sub.context["on_behalf_of"] == str(anna)
    assert sub.context["trigger_id"] == str(trigger_id)
