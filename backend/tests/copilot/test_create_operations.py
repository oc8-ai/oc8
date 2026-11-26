"""department.create / agent.create -- the Copilot's only two operations that
bootstrap a NEW resource rather than adjust an existing one. Still proposal-
only: apply_proposal is the one path that ever writes the real row."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def test_department_create_proposal_applies_to_a_real_row(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "department.create", "name": "Marketing", "goal": "Grow pipeline"}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        dept = (
            await db.execute(
                select(m.Department).where(
                    m.Department.tenant_id == acme_tenant, m.Department.name == "Marketing"
                )
            )
        ).scalar_one()
        assert dept.goal == "Grow pipeline"
        # Same department-tier memory default POST /departments uses -- an
        # empty grant here would silently leave every agent hired into this
        # department unable to remember anything.
        assert dept.frame["memory"]["department"] == ["read", "write"]


async def test_agent_create_proposal_applies_into_an_existing_department(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        dept_id = dept.id

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.create",
                    "departmentId": str(dept_id),
                    "name": "Nora",
                    "roleTitle": "SDR",
                    "mission": "Qualify inbound leads",
                }
            ],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        # Scoped to this test's department: ACME is shared across the suite and
        # other tests commit agents named "Nora" into it.
        agent = (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.tenant_id == acme_tenant,
                    m.Agent.department_id == dept_id,
                    m.Agent.name == "Nora",
                )
            )
        ).scalar_one()
        assert agent.department_id == dept_id
        assert agent.mission == "Qualify inbound leads"
        # No hire-approval flag set on the tenant -> not gated.
        assert agent.status == "idle"
        store = (
            await db.execute(select(m.MemoryStore).where(m.MemoryStore.owner_id == agent.id))
        ).scalar_one_or_none()
        assert store is not None


async def test_agent_create_rejects_a_department_that_does_not_exist(
    app_session, acme_tenant
) -> None:
    """The one thing that actually can go stale between propose and apply for
    a create operation: the department it targets. target_revision() returns
    None unconditionally for AgentCreate (nothing to compare), so this check
    has to live in apply_operation itself."""
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.create",
                    "departmentId": str(uuid.uuid4()),
                    "name": "Ghost",
                }
            ],
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_a_gated_agent_create_proposal_raises_the_hire_request(app_session) -> None:
    """Security scan: with hire approval required, the Copilot's agent.create
    left a pending agent with no hire request -- nobody could ever approve it.
    It raises one, exactly as the agent-create route does."""
    tenant = uuid.uuid4()
    actor = _actor(tenant)
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant, slug=f"t{tenant.hex[:6]}", name="T", tier="standard", region="eu",
                settings={"require_hire_approval": True},
            )
        )
        dept = m.Department(tenant_id=tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        proposal = await create_proposal(
            db,
            actor,
            [{"type": "agent.create", "departmentId": str(dept.id), "name": "Nora"}],
        )
        assert (await apply_proposal(db, proposal.id, actor)).status == "applied"
        agent = (
            await db.execute(select(m.Agent).where(m.Agent.department_id == dept.id))
        ).scalar_one()
        assert agent.status == "pending_approval"
        requests = (
            await db.execute(
                select(m.ApprovalRequest).where(
                    m.ApprovalRequest.agent_id == agent.id,
                    m.ApprovalRequest.action_type == "hire_agent",
                )
            )
        ).scalars().all()
        assert len(requests) == 1
