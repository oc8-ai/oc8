"""
`ApprovalRequest.record_url` -- the deep link to the record an approval is
about (design 2026-09-27-ai-workplace-collaboration-design.md §6).

Optional by construction: an approval raised without one is identical to what
every raiser produced before this column existed. That is the property the
whole feature rests on -- a tool pack that declares no URL shape must not be
able to stop an approval from being raised.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.approvals import raise_approval
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _agent(db: object, tenant: uuid.UUID) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name="Vertrieb")
    db.add(dept)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
    db.add(agent)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return agent


async def test_a_resolved_url_is_stored_on_the_row(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        approval = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="tool_send",
            title="Nora wants to call create_record",
            record_url="https://odoo.example.com/odoo/sale.order/42",
        )
        assert approval.record_url == "https://odoo.example.com/odoo/sale.order/42"
        reread = await db.get(m.ApprovalRequest, approval.id)
        assert reread is not None
        assert reread.record_url == "https://odoo.example.com/odoo/sale.order/42"


async def test_an_approval_raised_without_one_is_null_and_still_pending(
    app_session: AppSessionFactory,
) -> None:
    """The degradation path, asserted rather than assumed: no template, no
    link, no error, and the approval is a perfectly ordinary pending row."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        approval = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="tool_send",
            title="Nora wants to call create_record",
        )
        assert approval.record_url is None
        assert approval.status == "pending"
