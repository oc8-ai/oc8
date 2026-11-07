"""Where an approval belongs is decided once, when it is raised.

`approval_request.department_id` is denormalised rather than joined through the
agent, and both source designs converged on the same two reasons:

* moving an agent between departments must not drag its pending approvals with
  it -- somebody is looking at that queue right now;
* a join is a second place the messenger fan-out would forget the term.

`Task.department_id` is deliberately never consulted. The agent is authoritative:
`ApprovalRequest.agent_id` is NOT NULL and `Agent.department_id` is NOT NULL, so
the derivation always succeeds, whereas `task_id` is nullable and a held tool
call raised outside a task would land nowhere.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.approvals import raise_approval
from oc8.approvals.repo import visible_approvals
from oc8.approvals.service import DERIVE
from oc8.auth.principal import Principal
from oc8.authz.permissions import SEAT_APPROVER
from oc8.authz.scope import HumanActor, scope_for_principal
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "oc8"

#: The one place a department-less approval may be born. A tenant-scope budget
#: breach is genuinely company-wide: there is no department it belongs to, and
#: filing it under the breaching agent's would hide the company's problem inside
#: one team's queue.
#:
#: `approvals/service.py` passes one for the other reason this file exists: the
#: department stage of a member-routed approval (D3a) copies the department the
#: member stage was PINNED to. Re-deriving it would read the agent's department
#: now, which is exactly the drag-along this test forbids.
_MAY_OVERRIDE = {"approvals/service.py", "metering/budget.py"}


def _subject_uuid(subject: str) -> uuid.UUID:
    try:
        return uuid.UUID(subject)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


async def _seated(db: Any, tenant: uuid.UUID, subject: str, department_id: uuid.UUID) -> HumanActor:
    member = m.OrgMember(
        tenant_id=tenant,
        subject=subject,
        subject_uuid=_subject_uuid(subject),
        display_name=subject,
    )
    db.add(member)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=member.id,
            department_id=department_id,
            seat_role=SEAT_APPROVER,
        )
    )
    await db.flush()
    principal = Principal(subject=subject, tenant_id=tenant, role="member")
    resolved, scope = await scope_for_principal(db, principal)
    assert resolved is not None
    return HumanActor(principal=principal, member=resolved, scope=scope)


async def test_moving_an_agent_leaves_its_pending_approvals_where_they_were(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = m.Department(tenant_id=tenant, name="Vertrieb")
        engineering = m.Department(tenant_id=tenant, name="Entwicklung")
        db.add_all([sales, engineering])
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=sales.id, name="Nora")
        db.add(agent)
        await db.flush()

        approval = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="tool_send",
            title="Angebot Gartenholz GmbH",
            payload={"tool": "odoo.send_quotation", "arguments": {}},
        )
        assert approval.department_id == sales.id, (
            "the department is derived from the agent, not left for a caller to pass"
        )

        # The reorganisation. Nothing about the pending decision changes.
        agent.department_id = engineering.id
        await db.flush()

        in_sales = await _seated(db, tenant, "hos", sales.id)
        in_engineering = await _seated(db, tenant, "cto", engineering.id)

        reloaded = await db.get(m.ApprovalRequest, approval.id)
        assert reloaded is not None
        assert reloaded.department_id == sales.id

        assert [a.id for a in await visible_approvals(db, actor=in_sales, status="pending")] == [
            approval.id
        ]
        assert await visible_approvals(db, actor=in_engineering, status="pending") == [], (
            "moving the agent handed a pending decision to a department that never saw it raised"
        )


async def test_only_a_tenant_scope_budget_incident_has_no_department(
    app_session: AppSessionFactory,
) -> None:
    """NULL means tenant-wide, and it has exactly one producer.

    Three assertions, because each closes a different way of losing this:
    the default is a SENTINEL (a default of `None` would make every approval
    tenant-wide and invisible to every seat); the explicit override does still
    produce NULL; and no module in `src/` except the budget path passes it.
    """
    default = inspect.signature(raise_approval).parameters["department_id"].default
    assert default is DERIVE, (
        "defaulting to None would silently file every approval as company-wide, "
        "where only an org_admin can see it -- and nothing would error"
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        department = m.Department(tenant_id=tenant, name="Vertrieb")
        db.add(department)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=department.id, name="Nora")
        db.add(agent)
        await db.flush()

        derived = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="tool_send",
            title="Angebot",
        )
        assert derived.department_id == department.id

        company_wide = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=agent.id,
            action_type="budget_incident",
            title="Token budget exceeded — tenant",
            department_id=None,
        )
        assert company_wide.department_id is None

        # The seated approver sees his own, never the company's.
        hos = await _seated(db, tenant, "hos", department.id)
        visible = await visible_approvals(db, actor=hos, status="pending")
        assert [a.id for a in visible] == [derived.id]

    overriders = _modules_overriding_the_department()
    assert overriders == _MAY_OVERRIDE, (
        f"these modules pass department_id to raise_approval: {sorted(overriders)}; "
        f"only {sorted(_MAY_OVERRIDE)} may"
    )

    # And the budget path must go THROUGH the funnel: a direct
    # `ApprovalRequest(...)` there (as at `metering/budget.py:284` today) is a
    # department-less approval that also never reaches a messenger.
    budget = (SRC / "metering" / "budget.py").read_text()
    assert not _constructs_an_approval(budget), (
        "metering/budget.py still builds an ApprovalRequest by hand, so its "
        "incident is neither announced nor filed"
    )


def _modules_overriding_the_department() -> set[str]:
    """Every module in `src/oc8` that passes `department_id=` to `raise_approval`."""
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            name = name or (node.func.id if isinstance(node.func, ast.Name) else None)
            if name != "raise_approval":
                continue
            if any(kw.arg == "department_id" for kw in node.keywords):
                found.add(path.relative_to(SRC).as_posix())
    return found


def _constructs_an_approval(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else "")
        )
        if name == "ApprovalRequest":
            return True
    return False


def test_the_source_sweeps_here_can_actually_see_what_they_look_for() -> None:
    """A sweep that silently matched nothing would pass for ever.

    Both helpers are checked against source they are known to hold an opinion
    about, rather than against the repository's current state -- which is what
    they are for and what changes under them.
    """
    assert _constructs_an_approval("x = ApprovalRequest(tenant_id=1)")
    assert _constructs_an_approval("x = m.ApprovalRequest(tenant_id=1)")
    assert not _constructs_an_approval("x = raise_approval(db, tenant_id=1)")
    # `raise_approval` is called from at least the five raisers, so a walker
    # that found no calls at all is broken rather than reassuring.
    calls = 0
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else (func.id if isinstance(func, ast.Name) else "")
                )
                if name == "raise_approval":
                    calls += 1
    assert calls >= 5, f"only found {calls} raise_approval calls; the walk is broken"
