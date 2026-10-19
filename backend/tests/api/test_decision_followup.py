"""A human's decision becomes work the agent performs.

The agent does not wait for it (see tests/agents/test_request_decision.py), so
something has to carry the decision back. That something is a fresh run: the
decision is turned into an instruction and enqueued for the same agent, which is
the same intake path a cron trigger or an operator uses.

Without this half, deciding in the inbox would be a dead end -- the human clicks,
the record says "approved", and nothing ever happens.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.tenants.activity import set_tenant_activity_gate
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _pending(db: Any, tenant: uuid.UUID, **payload: Any) -> tuple[uuid.UUID, uuid.UUID]:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Sina")
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=agent.department_id,
        assigned_agent_id=agent.id,
        title="Ticket-Eingang",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    approval = m.ApprovalRequest(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        action_type="decision",
        title="Sollen wir die doppelte Abbuchung erstatten?",
        detail="Ticket #42, Kunde Travis Mendoza, 249,00 EUR doppelt abgebucht.",
        payload={
            "options": [
                {"key": "full", "label": "Voll erstatten", "detail": "249,00 EUR zurück"},
                {"key": "decline", "label": "Ablehnen", "detail": "Buchung war korrekt"},
            ],
            "recommendation": "full",
            **payload,
        },
        status="pending",
    )
    db.add(approval)
    await db.flush()
    return approval.id, agent.id


async def _decide(tenant: uuid.UUID, approval_id: uuid.UUID, body: dict[str, Any]) -> Any:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                f"/api/v1/approvals/{approval_id}/decision", json=body, headers=_h(tenant)
            )


async def _runs(db: Any, agent_id: uuid.UUID) -> list[m.AgentRun]:
    from sqlalchemy import select

    return list(
        (await db.execute(select(m.AgentRun).where(m.AgentRun.agent_id == agent_id)))
        .scalars()
        .all()
    )


async def test_a_chosen_option_reaches_the_agent_as_a_new_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        approval_id, agent_id = await _pending(db, tenant)
        await db.commit()

    r = await _decide(tenant, approval_id, {"decision": "approve", "option": "full"})
    assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        runs = await _runs(db, agent_id)
        assert len(runs) == 1, "the decision must produce exactly one follow-up run"
        task_text = runs[0].context["task"]
        # The instruction has to stand on its own: the agent starts a fresh
        # conversation and remembers nothing of the run that asked.
        assert "Voll erstatten" in task_text
        assert "Ticket #42" in task_text, "the original basis travels with it"

        decided = await db.get(m.ApprovalRequest, approval_id)
        assert decided is not None
        assert decided.status == "approved"
        assert decided.decision_option == "full"


async def test_free_text_travels_even_without_an_option(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The operator may want something none of the options offered."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        approval_id, agent_id = await _pending(db, tenant)
        await db.commit()

    r = await _decide(
        tenant,
        approval_id,
        {"decision": "approve", "reason": "Nur 50% erstatten, Kunde ist Neukunde."},
    )
    assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        runs = await _runs(db, agent_id)
        assert len(runs) == 1
        assert "Nur 50% erstatten" in runs[0].context["task"]


async def test_a_rejection_also_comes_back(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Rejecting is a decision too: somebody still has to tell the customer. A
    silent reject would leave the ticket hanging with the customer waiting."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        approval_id, agent_id = await _pending(db, tenant)
        await db.commit()

    r = await _decide(tenant, approval_id, {"decision": "reject", "reason": "Kulanz erschöpft."})
    assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        runs = await _runs(db, agent_id)
        assert len(runs) == 1
        text = runs[0].context["task"]
        assert "abgelehnt" in text.lower()
        assert "Kulanz erschöpft." in text


async def test_an_unknown_option_is_refused(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Otherwise a typo in the UI silently becomes an instruction the agent
    invents a meaning for."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        approval_id, agent_id = await _pending(db, tenant)
        await db.commit()

    r = await _decide(tenant, approval_id, {"decision": "approve", "option": "nonsense"})
    assert r.status_code == 422, r.text

    async with app_session(tenant) as db:
        assert await _runs(db, agent_id) == []
        still = await db.get(m.ApprovalRequest, approval_id)
        assert still is not None and still.status == "pending"


def _gate_saying_no_for(tenant: uuid.UUID):  # type: ignore[no-untyped-def]
    async def gate(tenant_id: uuid.UUID) -> bool:
        return tenant_id != tenant

    return gate


async def test_a_decision_for_an_inactive_organisation_starts_no_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        approval_id, agent_id = await _pending(db, tenant)
        await db.commit()
    set_tenant_activity_gate(_gate_saying_no_for(tenant))

    r = await _decide(tenant, approval_id, {"decision": "approve", "option": "full"})
    assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        assert await _runs(db, agent_id) == []
