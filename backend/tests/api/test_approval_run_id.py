"""`runId` on a listed approval (run step timeline, Task 10).

The needs-me queue sorts by how much work an item blocks and renders the
blocked run's own step timeline inside the row. Neither is possible from a
task id: `ApprovalRequest` has no run_id column, so the run has to be
resolved through the task -- once per page, not once per row.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.api.v1._serializers import approval_to_dto, resolve_approval_names
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_an_approval_raised_inside_a_run_carries_that_runs_id(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
        db.add(agent)
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept.id,
            assigned_agent_id=agent.id,
            title="Offer for Acme",
            state="in_progress",
        )
        db.add(task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            task_id=task.id,
            state="waiting_for_approval",
            context={},
        )
        db.add(run)
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            department_id=dept.id,
            task_id=task.id,
            action_type="tool_send",
            payload={"tool": "create_record", "arguments": {}},
            status="pending",
            title="Nora wants to call create_record",
            detail="above the limit",
        )
        db.add(approval)
        await db.flush()

        names = await resolve_approval_names(db, [approval])
        dto = approval_to_dto(approval, names)
        expected_run_id = str(run.id)

    assert dto.run_id == expected_run_id


async def test_an_approval_with_no_task_carries_no_run_id(
    app_session: AppSessionFactory,
) -> None:
    """A tenant-wide budget incident blocks no particular run, and the queue
    sorts it below one that does."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Nora")
        db.add(agent)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            department_id=None,
            task_id=None,
            action_type="budget_incident",
            payload={},
            status="pending",
            title="Budget exhausted",
            detail="",
        )
        db.add(approval)
        await db.flush()

        names = await resolve_approval_names(db, [approval])
        dto = approval_to_dto(approval, names)

    assert dto.run_id is None


async def test_the_single_row_door_still_works_without_names(
    app_session: AppSessionFactory,
) -> None:
    """`approval_to_dto(row)` with no ApprovalNames is the decision-response
    path (api/v1/approvals.py:79) -- it must keep working and simply report no
    run rather than raising a KeyError."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Nora")
        db.add(agent)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=agent.id,
            task_id=uuid.uuid4(),
            action_type="tool_send",
            payload={},
            status="approved",
            title="x",
            detail="",
        )
        db.add(approval)
        await db.flush()
        dto = approval_to_dto(approval)

    assert dto.run_id is None
