"""department.update / department.delete / department.restore -- CRUD parity
with PATCH/DELETE/POST-restore /departments/{id}, reused through
apply_operation rather than the REST route."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def test_department_update_applies_partial_fields(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", goal="Grow", frame={})
        db.add(dept)
        await db.flush()
        dept_id = dept.id

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "department.update",
                    "departmentId": str(dept_id),
                    "goal": "Grow faster",
                }
            ],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        await db.refresh(dept)
        assert dept.name == "Sales"
        assert dept.goal == "Grow faster"


async def test_department_delete_archives_when_it_has_live_agents(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=acme_tenant, department_id=dept.id, name="Nora", definition={})
        db.add(agent)
        await db.flush()
        dept_id = dept.id

        proposal = await create_proposal(
            db,
            actor,
            [{"type": "department.delete", "departmentId": str(dept_id)}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        await db.refresh(dept)
        assert dept.deleted_at is not None


async def test_department_restore_clears_deleted_at(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        import datetime as dt

        dept = m.Department(
            tenant_id=acme_tenant, name="Sales", frame={}, deleted_at=dt.datetime.now(tz=dt.UTC)
        )
        db.add(dept)
        await db.flush()
        dept_id = dept.id

        proposal = await create_proposal(
            db,
            actor,
            [{"type": "department.restore", "departmentId": str(dept_id)}],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        await db.refresh(dept)
        assert dept.deleted_at is None


async def test_department_update_rejects_a_stale_revision(app_session, acme_tenant) -> None:
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
                    "type": "department.update",
                    "departmentId": str(dept_id),
                    "name": "Renamed",
                }
            ],
        )
        dept.name = "Changed underneath"
        await db.flush()

        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)
