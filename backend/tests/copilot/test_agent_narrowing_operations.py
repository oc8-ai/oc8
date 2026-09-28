"""agent.narrowing.set / agent.narrowing.reset -- mirrors PUT /agents/{id}/
narrowing and POST /agents/{id}/narrowing/{key}/reset exactly, through
apply_operation instead of the REST route."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def test_narrowing_set_rejects_a_grant_the_department_frame_does_not_allow(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        # `narrowing_within_frame` only flags a KEY THE FRAME ALREADY GRANTS
        # being widened beyond what the frame allows for it -- a key entirely
        # absent from the frame is an agent-exclusive grant, not a violation
        # (see `pdp.narrowing_within_frame`'s own docstring and
        # `tests/authz/test_pdp.py`'s
        # `test_narrowing_within_frame_does_not_flag_a_tool_absent_from_the_frame`).
        # So the frame here grants `github_mcp` with `modify: False`, and the
        # proposed narrowing asks for `modify: True` -- a real widening.
        dept = m.Department(
            tenant_id=acme_tenant,
            name="Sales",
            frame={"tools": {"github_mcp": {"enabled": True, "read": True, "modify": False}}},
        )
        db.add(dept)
        await db.flush()
        # A name distinct from other copilot tests' "Nora" -- `app_session`
        # commits, and several other files (`test_create_operations.py`,
        # `test_department_operations.py`, `test_proposal_listing.py`) look
        # up an agent by `name == "Nora"` in this same session-scoped
        # `acme_tenant`; reusing that name here would make one of those
        # lookups raise `MultipleResultsFound` depending on file/run order.
        agent = m.Agent(tenant_id=acme_tenant, department_id=dept.id, name="Petra", definition={})
        db.add(agent)
        await db.flush()

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.narrowing.set",
                    "agentId": str(agent.id),
                    "narrowing": {
                        "tools": {"github_mcp": {"enabled": True, "read": True, "modify": True}}
                    },
                }
            ],
        )
        # narrowing_within_frame must reject this -- same violation
        # set/narrowing's own REST route produces, surfaced here as
        # InvalidOperation -> proposal "rejected".
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_narrowing_reset_removes_one_key_and_its_override_marker(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=acme_tenant,
            department_id=dept.id,
            name="Petra",
            definition={},
            narrowing={"tools": {"github_mcp": {"enabled": False}}},
            narrowing_overridden_keys=["github_mcp"],
        )
        db.add(agent)
        await db.flush()

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.narrowing.reset",
                    "agentId": str(agent.id),
                    "connectionName": "github_mcp",
                }
            ],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        await db.refresh(agent)
        assert "github_mcp" not in agent.narrowing.get("tools", {})
        assert "github_mcp" not in (agent.narrowing_overridden_keys or [])


async def test_narrowing_reset_rejects_a_key_not_currently_set(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=acme_tenant, department_id=dept.id, name="Petra", definition={})
        db.add(agent)
        await db.flush()

        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "agent.narrowing.reset",
                    "agentId": str(agent.id),
                    "connectionName": "github_mcp",
                }
            ],
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)
