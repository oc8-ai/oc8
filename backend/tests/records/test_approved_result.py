"""The record an approved call produced is stored on its approval (spec §5
item 5), so the card can say "Created: S00043 ↗"."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.approvals import raise_approval
from oc8.records.approvals import note_approved_result
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

URL = "https://odoo.example.com/odoo/sale.order/43"
ARGS = {"model": "sale.order", "values": {"partner_id": 7}}


async def _approved(db: AsyncSession, tenant: uuid.UUID) -> tuple[m.ApprovalRequest, uuid.UUID]:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Angebot",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    approval = await raise_approval(
        db,
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        action_type="tool_send",
        title="Nora wants to call create_record",
        payload={"tool": "create_record", "arguments": ARGS},
    )
    approval.status = "approved"
    await db.flush()
    return approval, task.id


async def test_the_approved_call_s_record_lands_on_its_approval(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        approval, task_id = await _approved(db, tenant)
        await note_approved_result(
            db,
            tenant_id=tenant,
            task_id=task_id,
            tool_names=("create_record",),
            arguments=ARGS,
            urls=[URL, "https://odoo.example.com/odoo/sale.order/44"],
        )
        await db.flush()
        await db.refresh(approval)
        assert approval.result_url == URL


async def test_a_different_call_leaves_the_approval_alone(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        approval, task_id = await _approved(db, tenant)
        await note_approved_result(
            db,
            tenant_id=tenant,
            task_id=task_id,
            tool_names=("create_record",),
            arguments={"model": "sale.order", "values": {"partner_id": 8}},
            urls=[URL],
        )
        await db.flush()
        await db.refresh(approval)
        assert approval.result_url is None


async def test_related_links_are_stored_on_a_raised_approval(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    related = [{"label": "Kontakt #7", "url": "https://odoo.example.com/odoo/res.partner/7"}]
    async with app_session(tenant) as db:
        approval, _ = await _approved(db, tenant)
        assert approval.related_links == []
        other = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=approval.agent_id,
            action_type="tool_send",
            title="again",
            related_links=related,
        )
        await db.refresh(other)
        assert other.related_links == related


async def test_a_failing_lookup_never_raises_into_the_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_a: object, **_kw: object) -> list[m.ApprovalRequest]:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr("oc8.records.approvals.approved_awaiting_result", _boom)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await note_approved_result(
            db,
            tenant_id=tenant,
            task_id=uuid.uuid4(),
            tool_names=("create_record",),
            arguments=ARGS,
            urls=[URL],
        )
