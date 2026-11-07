"""Migration 0107: `member_guardrail` (a member's own tightening of one agent's
tools, D3a) and the approval routing columns."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_member_guardrail_round_trip_and_cascade(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Sales agent")
        member = m.OrgMember(tenant_id=tenant, subject="anna@x.de", subject_uuid=uuid.uuid4())
        db.add_all([agent, member])
        await db.flush()
        guardrail = m.MemberGuardrail(
            tenant_id=tenant,
            member_id=member.id,
            agent_id=agent.id,
            policy={"tools": {"gmail": {"enabled": False}}},
        )
        db.add(guardrail)
        await db.flush()
        assert guardrail.config_revision == 1
        guardrail.policy = {"tools": {}}
        await db.flush()
        await db.refresh(guardrail)
        assert guardrail.config_revision == 2

        await db.execute(text("DELETE FROM agent WHERE id = :id"), {"id": agent.id})
        rows = (await db.execute(select(m.MemberGuardrail))).scalars().all()
        assert rows == []


async def test_approval_request_has_routing_columns(app_session: AppSessionFactory) -> None:
    async with app_session(uuid.uuid4()) as db:
        cols = (
            (
                await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'approval_request'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {"assignee_member_id", "previous_approval_id"} <= set(cols)
