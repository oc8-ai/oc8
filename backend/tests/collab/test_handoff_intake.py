"""An accepted handoff lands on the team lead's desk as work.

§14a.3 says an intake "materializes as a new task assigned to the team-lead
agent, who decomposes it per delegation". It did not: `accept_handoff` moved a
status and recorded a task id the CALLER had to supply, so accepting produced a
row and nothing to do. Measured before the fix: 413 tasks, none delegated, and
no run had ever had a delegation source.

This is also the answer to what a lead is for. Two agents already share one
queue — the record claim keeps them apart, and a coordinator handing out
tickets would cost two runs each and buy nothing. A lead earns its place where
work arrives as ONE lump somebody has to cut up, which is what a handoff is.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.collab.intake import brief, route_to_team_lead
from oc8.runtime.queue import RunQueue
from oc8.tenants.activity import set_tenant_activity_gate
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture
def queue(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Its own stream per test: enqueueing is what turns the task into work, so
    it cannot be stubbed away without testing something else."""
    q = RunQueue(redis_url, key=f"test:{uuid.uuid4()}")
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: q)
    return q


async def _department(
    db: Any, tenant: uuid.UUID, *, with_lead: bool
) -> tuple[m.Department, m.Agent | None]:
    dept = m.Department(tenant_id=tenant, name="Kundenservice", frame={})
    db.add(dept)
    await db.flush()
    lead: m.Agent | None = None
    if with_lead:
        lead = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Sina", status="idle",
            is_team_lead=True, narrowing={}, definition={}, presentation={},
        )
        db.add(lead)
        await db.flush()
        dept.team_lead_agent_id = lead.id
        await db.flush()
    return dept, lead


async def _handoff(
    db: Any, tenant: uuid.UUID, target: m.Department, payload: dict[str, Any]
) -> m.Handoff:
    htype = m.HandoffType(tenant_id=tenant, name="sales.deal.won", payload_schema={})
    source = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add_all([htype, source])
    await db.flush()
    handoff = m.Handoff(
        tenant_id=tenant, handoff_type_id=htype.id,
        source_department_id=source.id, target_department_id=target.id,
        payload=payload, status="accepted", created_by=uuid.uuid4(),
    )
    db.add(handoff)
    await db.flush()
    return handoff


async def test_the_lead_gets_a_task_and_a_run(
    app_session: AppSessionFactory, queue: Any
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept, lead = await _department(db, tenant, with_lead=True)
        assert lead is not None
        handoff = await _handoff(db, tenant, dept, {"kunde": "Acme", "produkt": "Gartenhaus"})

        task_id = await route_to_team_lead(db, handoff, tenant_id=tenant)
        assert task_id is not None
        assert handoff.target_task_id == task_id, "the source department can follow it"

        task = await db.get(m.Task, task_id)
        assert task is not None
        assert task.assigned_agent_id == lead.id, "the LEAD, not just anybody"
        assert task.department_id == dept.id

    async with app_session(tenant) as db:
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == lead.id))
        ).scalars().first()
        assert run is not None, "a task nobody runs is a row, not work"
        assert run.source == "handoff", "the origin survives into the audit trail"
        assert "Acme" in run.context["task"], "and the lead is told what arrived"
        # Observed live before this was asserted: the run carried no task, so
        # the engine opened one of its OWN on start and the board showed the
        # handoff twice -- once as "Übergabe: …" that nobody was working, once
        # titled with the entire briefing text.
        assert run.task_id == task_id, "the run works the task the handoff created"

    async with app_session(tenant) as db:
        tasks = (
            (await db.execute(select(m.Task).where(m.Task.assigned_agent_id == lead.id)))
            .scalars()
            .all()
        )
        assert len(tasks) == 1, "one handoff is one piece of work on the board"


async def test_accepting_the_same_handoff_twice_starts_one_run(
    app_session: AppSessionFactory, queue: Any
) -> None:
    """A redelivery must not set the department to work on it a second time."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept, lead = await _department(db, tenant, with_lead=True)
        handoff = await _handoff(db, tenant, dept, {"kunde": "Acme"})
        await route_to_team_lead(db, handoff, tenant_id=tenant)
    async with app_session(tenant) as db:
        fresh = await db.get(m.Handoff, handoff.id)
        assert fresh is not None
        await route_to_team_lead(db, fresh, tenant_id=tenant)

    async with app_session(tenant) as db:
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == lead.id)))
            .scalars()
            .all()
        )
        assert len(runs) == 1


async def test_a_department_without_a_lead_is_left_alone(
    app_session: AppSessionFactory,
) -> None:
    """Accepted, and visibly on nobody. Inventing an assignee would hide that
    the work has no owner — which is the failure this is meant to prevent."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept, _ = await _department(db, tenant, with_lead=False)
        handoff = await _handoff(db, tenant, dept, {"kunde": "Acme"})
        assert await route_to_team_lead(db, handoff, tenant_id=tenant) is None
        assert handoff.target_task_id is None


async def test_the_brief_says_it_is_one_job_and_carries_the_payload() -> None:
    """The lead has to know this is not a ticket — a queue procedure applied to
    a handoff would answer the sending department instead of decomposing it."""
    handoff = m.Handoff(
        tenant_id=uuid.uuid4(), handoff_type_id=uuid.uuid4(),
        source_department_id=uuid.uuid4(), target_department_id=uuid.uuid4(),
        payload={"kunde": "Acme", "wert": "12.400 EUR"}, created_by=uuid.uuid4(),
    )
    text = brief(handoff, "sales.deal.won")
    assert "EIN Auftrag" in text
    assert "delegate_task" in text
    assert "Acme" in text and "12.400 EUR" in text, "verbatim: summarising it here would"


def _gate_saying_no_for(tenant: uuid.UUID):  # type: ignore[no-untyped-def]
    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    return gate


async def test_a_handoff_accepted_for_an_inactive_organisation_starts_no_run(
    app_session: AppSessionFactory, queue: Any
) -> None:
    tenant = uuid.uuid4()
    set_tenant_activity_gate(_gate_saying_no_for(tenant))
    async with app_session(tenant) as db:
        dept, lead = await _department(db, tenant, with_lead=True)
        assert lead is not None
        handoff = await _handoff(db, tenant, dept, {"kunde": "Acme"})

        await route_to_team_lead(db, handoff, tenant_id=tenant)

    async with app_session(tenant) as db:
        runs = (
            (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == lead.id)))
            .scalars()
            .all()
        )
        assert runs == []
