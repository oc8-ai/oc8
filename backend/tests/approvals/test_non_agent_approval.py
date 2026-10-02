"""Task C2: the raising side already has an escape hatch for a non-agent caller.

`ApprovalRequest.agent_id` is `NOT NULL` and `_department_of`
(`approvals/service.py`) derives the department from `Agent.department_id`
unless `department_id` is passed explicitly. The workflow spec's §4.8 rules
that the column should eventually become nullable rather than adopt a
sentinel agent id -- a fake id in an attribution trail is a worse lie than a
nullable column with a CHECK -- but that migration is deliberately not this
task's job.

This file only proves the premise that ruling rests on: that
`department_id` can be supplied directly today, bypassing the `Agent` lookup
entirely, with no schema change. If it did not, `_department_of` would be the
remaining agent-shaped coupling on the raising side rather than the `NOT
NULL` column, and §4.8 would need to be rewritten before anyone builds on it.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.approvals import raise_approval
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_raise_approval_accepts_an_explicit_department_and_skips_derivation(
    app_session: AppSessionFactory,
) -> None:
    """`department_id` can be supplied directly, bypassing the `Agent` lookup.

    Proves the derivation is the only agent-shaped coupling on the raising
    side -- the `NOT NULL` column is the remaining one, and it is a migration,
    not a redesign.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agents_department = m.Department(tenant_id=tenant, name="Vertrieb")
        workflow_department = m.Department(tenant_id=tenant, name="Operations")
        db.add_all([agents_department, workflow_department])
        await db.flush()

        # `ApprovalRequest.agent_id` is NOT NULL today, so a non-agent caller
        # still has to name an agent row -- that column, not the department
        # derivation, is the remaining coupling this test deliberately leaves
        # alone (see the module docstring and §4.8).
        agent = m.Agent(tenant_id=tenant, department_id=agents_department.id, name="Nora")
        db.add(agent)
        await db.flush()

        approval = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="tool_send",
            title="raised by a workflow node, not an agent run",
            department_id=workflow_department.id,
        )

        assert approval.department_id == workflow_department.id, (
            "an explicit department_id must win over derivation from Agent.department_id"
        )
        assert approval.department_id != agent.department_id, (
            "the point of the test is that the explicit value differs from what "
            "derivation would have produced"
        )
