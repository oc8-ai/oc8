"""`approval:decide` is not one right. It is polymorphic on `action_type`.

Reproduced end to end before `EFFECT_PERMISSIONS` existed, with a token whose
role is `member` (`frozenset()`) holding one `dept_approver` seat:

    GET  /budgets                              -> 403
    POST /approvals/{budget_incident}/decision -> 200
    budget.override_until                      -> 2026-09-01   <-- a month's spend
    POST /approvals/{hire_agent}/decision      -> 200
    agent.deleted_at                           -> set          <-- an agent, deleted

`resolve_budget_incident` sets `override_until` and un-pauses the frozen scope --
the same act as `PUT /budgets`, which is `budget:manage`. `resolve_hire_agent`
activates a pending agent or soft-deletes it -- `agent:manage`. Neither is in
`SEAT_PERMISSIONS`, and `tests/authz/test_seat_vocabulary.py` did not see it,
because it asserts the STRINGS are absent from the seat dict and they are: the
effect is reached through the row, not through the vocabulary.

This slice is also what routed the first one into a seat-holder's queue.
`metering/budget.py` now files the incident under the breaching department; it
carried no department at all before, and the route was
`require_permission(APPROVAL_DECIDE)`, which a `member` token failed outright.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.approvals import EFFECT_PERMISSIONS
from oc8.auth import get_identity_provider
from oc8.authz.permissions import MEMBER_ROLE, ORG_ADMIN, SEAT_APPROVER
from oc8.authz.scope import subject_uuid_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


class _Office:
    tenant: uuid.UUID
    department: uuid.UUID
    agent: uuid.UUID
    incident: uuid.UUID
    hire: uuid.UUID
    held: uuid.UUID
    pending_agent: uuid.UUID


async def _office(app_session: AppSessionFactory) -> _Office:
    """One department, one seat-holder in it, and one approval of each kind
    FILED IN THAT DEPARTMENT -- so every refusal below is about the effect and
    never about the department."""
    office = _Office()
    office.tenant = uuid.uuid4()
    async with app_session(office.tenant) as db:
        dept = m.Department(tenant_id=office.tenant, name="Vertrieb")
        db.add(dept)
        await db.flush()
        office.department = dept.id

        agent = m.Agent(tenant_id=office.tenant, department_id=dept.id, name="Nora")
        newcomer = m.Agent(
            tenant_id=office.tenant,
            department_id=dept.id,
            name="Neu",
            status="pending_approval",
        )
        db.add_all([agent, newcomer])
        await db.flush()
        office.agent = agent.id
        office.pending_agent = newcomer.id

        budget = m.Budget(
            tenant_id=office.tenant,
            department_id=dept.id,
            hard_limit_tokens=1000,
        )
        db.add(budget)

        rows = {
            "incident": m.ApprovalRequest(
                tenant_id=office.tenant,
                agent_id=agent.id,
                department_id=dept.id,
                action_type="budget_incident",
                status="pending",
                title="Token-Budget überschritten",
                detail="",
                payload={"scope": "department", "department_id": str(dept.id)},
            ),
            "hire": m.ApprovalRequest(
                tenant_id=office.tenant,
                agent_id=newcomer.id,
                department_id=dept.id,
                action_type="hire_agent",
                status="pending",
                title="Hire agent: Neu",
                detail="",
                payload={"name": "Neu", "department_id": str(dept.id)},
            ),
            "held": m.ApprovalRequest(
                tenant_id=office.tenant,
                agent_id=agent.id,
                department_id=dept.id,
                action_type="tool_send",
                status="pending",
                title="Angebot Gartenholz GmbH",
                detail="",
                payload={"tool": "odoo.send_quotation", "arguments": {}},
            ),
        }
        db.add_all(list(rows.values()))

        member = m.OrgMember(
            tenant_id=office.tenant,
            subject="hos",
            subject_uuid=subject_uuid_for("hos"),
            display_name="Head of Sales",
        )
        db.add(member)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=office.tenant,
                member_id=member.id,
                department_id=dept.id,
                seat_role=SEAT_APPROVER,
            )
        )
        await db.flush()
        office.incident = rows["incident"].id
        office.hire = rows["hire"].id
        office.held = rows["held"].id
    return office


async def _reload(app_session: AppSessionFactory, tenant: uuid.UUID, model: Any, row_id: uuid.UUID):
    async with app_session(tenant) as db:
        return await db.get(model, row_id)


async def test_a_seat_holder_cannot_lift_his_own_departments_budget(
    app_session: AppSessionFactory,
) -> None:
    """403, and -- the assertion that actually matters -- `override_until` is
    still unset and the row is still pending afterwards.

    A route that applied the effect and THEN refused would pass a
    status-code-only test while the month's cap was already lifted.
    """
    office = await _office(app_session)
    headers = _headers(office.tenant, "hos", MEMBER_ROLE)

    async with _http() as http:
        # The control: he genuinely holds neither of the two rights this decision
        # exercises, which is what makes the 200 it used to give an escalation
        # rather than a shortcut. `GET /agents` is no longer part of this control
        # -- `agent:view` graduated into the seat view level (department-scoped-
        # agent-authority design, decision 2), so his `dept_approver` seat admits
        # him there on its own; that is READ graduating for any live seat, not
        # `budget:manage` or `agent:manage` reaching further than they were
        # composed to.
        assert (await http.get("/api/v1/budgets", headers=headers)).status_code == 403

        # He can SEE it -- it is his department's breach, filed in his queue by
        # this very slice -- which is why the refusal is 403 and not 404.
        listed = await http.get("/api/v1/approvals", headers=headers)
        assert listed.status_code == 200
        assert str(office.incident) in [r["id"] for r in listed.json()]

        refused = await http.post(
            f"/api/v1/approvals/{office.incident}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert refused.status_code == 403, refused.text
        assert "budget:manage" in refused.text

    row = await _reload(app_session, office.tenant, m.ApprovalRequest, office.incident)
    assert row is not None and row.status == "pending"
    assert row.decided_at is None and row.decided_by is None
    async with app_session(office.tenant) as db:
        budget = (
            await db.execute(select(m.Budget).where(m.Budget.department_id == office.department))
        ).scalar_one()
        assert budget.override_until is None, "a seat lifted the department's token cap"


async def test_a_seat_holder_cannot_bring_an_agent_into_service_or_delete_one(
    app_session: AppSessionFactory,
) -> None:
    """Creating an agent needs `agent:manage`; approving its hire request puts the
    same agent into service, and rejecting it soft-deletes the row."""
    office = await _office(app_session)
    headers = _headers(office.tenant, "hos", MEMBER_ROLE)

    async with _http() as http:
        for decision in ("approve", "reject"):
            refused = await http.post(
                f"/api/v1/approvals/{office.hire}/decision",
                json={"decision": decision, "reason": "nope"},
                headers=headers,
            )
            assert refused.status_code == 403, refused.text
            assert "agent:manage" in refused.text

    agent = await _reload(app_session, office.tenant, m.Agent, office.pending_agent)
    assert agent is not None
    assert agent.status == "pending_approval", "a seat hired an agent"
    assert agent.deleted_at is None, "a seat soft-deleted an agent"


async def test_the_held_tool_call_the_workspace_exists_for_still_decides(
    app_session: AppSessionFactory,
) -> None:
    """The guard against fixing an escalation by breaking the flagship.

    `tool_send` is the 3000-EUR gate the product is demonstrated on: a seat
    decides it, and nothing about this fix may touch that.
    """
    office = await _office(app_session)
    headers = _headers(office.tenant, "hos", MEMBER_ROLE)

    async with _http() as http:
        ok = await http.post(
            f"/api/v1/approvals/{office.held}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text

    row = await _reload(app_session, office.tenant, m.ApprovalRequest, office.held)
    assert row is not None and row.status == "approved"
    assert row.decided_by is not None


async def test_an_org_admin_still_resolves_both_kinds(app_session: AppSessionFactory) -> None:
    """The other half of the guard: the two action types are not simply broken.

    `org_admin` holds every permission, so the effect gate is transparent to the
    person who was always meant to answer these.
    """
    office = await _office(app_session)
    headers = _headers(office.tenant, "boss", ORG_ADMIN)

    async with _http() as http:
        assert (
            await http.post(
                f"/api/v1/approvals/{office.incident}/decision",
                json={"decision": "approve"},
                headers=headers,
            )
        ).status_code == 200
        assert (
            await http.post(
                f"/api/v1/approvals/{office.hire}/decision",
                json={"decision": "approve"},
                headers=headers,
            )
        ).status_code == 200

    async with app_session(office.tenant) as db:
        budget = (
            await db.execute(select(m.Budget).where(m.Budget.department_id == office.department))
        ).scalar_one()
        assert budget.override_until is not None
        assert budget.override_until > dt.datetime.now(tz=dt.UTC)
    agent = await _reload(app_session, office.tenant, m.Agent, office.pending_agent)
    assert agent is not None and agent.status == "idle"


def test_every_action_type_the_funnel_dispatches_on_is_classified() -> None:
    """The forcing function, and the reason this is not just three assertions.

    `decide_approval` dispatches on `approval.action_type` into five handlers.
    Two of them apply an effect gated elsewhere on a `:manage` permission, and
    nothing connected the two facts -- so a sixth kind of approval could be added
    with a handler that does anything at all and a seat-holder could trigger it.

    This reads the dispatch chain out of the SOURCE, so it cannot agree with a
    stale copy of the list: an `action_type` compared in `decide_approval` and
    missing from `EFFECT_PERMISSIONS` fails here, and the fix is for somebody to
    write down what approving it costs.
    """
    import ast
    import inspect
    import textwrap

    from oc8.approvals import service

    tree = ast.parse(textwrap.dedent(inspect.getsource(service.decide_approval)))
    dispatched: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "action_type"):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                dispatched.add(comparator.value)

    assert len(dispatched) >= 5, (
        f"the sweep found only {sorted(dispatched)} in decide_approval; it is no "
        "longer reading the dispatch chain and every assertion below is vacuous"
    )
    unclassified = dispatched - set(EFFECT_PERMISSIONS)
    assert not unclassified, (
        f"decide_approval resolves {sorted(unclassified)} and EFFECT_PERMISSIONS "
        "does not say what approving one costs. A seat carries four permissions "
        "and none of them is `:manage`; say which tenant-wide permission this "
        "action_type's handler actually applies, or None with the sentence saying "
        "why a seat is enough."
    )
    # And the two that are known to apply a management effect are still named,
    # so "classify everything as None" cannot satisfy the assertion above.
    assert EFFECT_PERMISSIONS["budget_incident"] == "budget:manage"
    assert EFFECT_PERMISSIONS["hire_agent"] == "agent:manage"
