"""A builder proposal may create the department its agent goes into, when no
department fits -- only for a member who may create departments, checked when
it is proposed and, as whoever applies it, when it is applied."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import draft_status, resolve_version
from oc8.api.mcp_gateway import _connections
from oc8.api.v1.copilot_builder import builder_dto
from oc8.approvals import EffectRefused, decide_approval
from oc8.auth import Principal
from oc8.authz.authority import authority_for_member, tenant_wide_read
from oc8.authz.permissions import DEPARTMENT, MANAGE, VIEW
from oc8.authz.permissions import perm as _perm
from oc8.authz.scope import HumanActor, scope_for_member
from oc8.copilot.builder import (
    apply_builder_proposal,
    create_builder_proposal,
    load_steps,
    request_builder_approval,
    resolve_builder_approval,
)
from oc8.copilot.builder_ops import (
    BuilderInvalid,
    builder_operation_data,
    flat_operations,
    parse_builder_operations,
    resolve_step,
)
from oc8.copilot.builder_tools import builder_options_text
from oc8.copilot.capabilities import new_department_frame
from oc8.copilot.models import CopilotProposal
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import BuilderOffice, builder_office
from tests.copilot.helpers import Seat, copilot_seat
from tests.copilot.test_builder_connections import (
    _install,
    _office_without_legacy,
    _org_login,
    _plugins_path,  # noqa: F401 -- the plugins directory, autouse
)
from tests.copilot.test_builder_tools import _call, _run

pytestmark = pytest.mark.asyncio
NOW = dt.datetime.now(tz=dt.UTC)


def _sales_agent(office: BuilderOffice, **extra: Any) -> dict[str, Any]:
    return {
        "newDepartment": {"name": "Vertrieb", "goal": "Verkauf und Angebote"},
        "name": "Angebotsassistent",
        "roleTitle": "Vertrieb",
        "mission": "Bereitet Angebote aus Odoo vor.",
        "modelId": str(office.model_id),
        **extra,
    }


async def _seat(db: AsyncSession, tenant: uuid.UUID, subject: str) -> Seat:
    """A member whose rights are their token's built-in role. A tenant-defined
    role never holds a `:manage` right (`NEVER_DELEGATABLE`), so creating
    departments is `org_admin`'s and `dept_manager`'s."""
    return await copilot_seat(db, tenant, subject, with_role=False)


async def _propose_as(
    db: AsyncSession,
    office: BuilderOffice,
    seat: Seat,
    ops: list[dict[str, Any]],
    *,
    role: str,
) -> CopilotProposal:
    member = await db.get(m.OrgMember, seat.member_id)
    assert member is not None
    scope = await scope_for_member(db, member, token_role=role)
    authority = await authority_for_member(db, member, token_role=role)
    return await create_builder_proposal(
        db,
        tenant_id=office.tenant,
        member=member,
        scope=scope,
        tenant_wide=tenant_wide_read(authority, _perm(DEPARTMENT, VIEW)),
        session_id=seat.session_id,
        operations=ops,
        now=NOW,
        may_create_departments=_perm(DEPARTMENT, MANAGE) in authority.tenant_wide,
    )


async def _human(db: AsyncSession, office: BuilderOffice, seat: Seat, role: str) -> HumanActor:
    member = await db.get(m.OrgMember, seat.member_id)
    assert member is not None
    return HumanActor(
        principal=Principal(subject=seat.subject, tenant_id=office.tenant, role=role),
        member=member,
        scope=await scope_for_member(db, member, token_role=role),
    )


async def test_propose_and_create_an_agent_in_a_new_department(
    app_session: AppSessionFactory,
) -> None:
    """The admin's Copilot proposes a sales agent although only other
    departments exist; "Create" makes the department with the default frame,
    the agent in it, publishes it -- and Odoo, an organisation login no frame
    lists, is usable by its runs."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp")
        await _org_login(db, tenant)
        run, task = await _run(db, office, chat_mode="agent")
        options = await _call(db, office, run, task, "builder_options")
        assert "New department: allowed" in options.output

        outcome = await _call(
            db, office, run, task, "propose_agent", **_sales_agent(office, systems=["odoo"])
        )
        assert not outcome.output.startswith("ERROR"), outcome.output
        proposal = (await db.execute(select(CopilotProposal))).scalar_one()
        steps = await load_steps(db, proposal)
        card = await builder_dto(
            db, proposal, steps, may_create=True, read_only=False, show_mission=True
        )
        assert card.new_department and card.department_name == "Vertrieb"
        assert card.department_id is None
        assert (
            await db.execute(select(m.Department).where(m.Department.name == "Vertrieb"))
        ).scalar_one_or_none() is None

        agent_id = await apply_builder_proposal(
            db,
            proposal_id=proposal.id,
            expect_status="draft",
            applied_by="admin",
            principal=Principal(subject="admin", tenant_id=tenant, role="org_admin"),
            now=proposal.created_at,
        )
        dept = (
            await db.execute(select(m.Department).where(m.Department.name == "Vertrieb"))
        ).scalar_one()
        assert dept.goal == "Verkauf und Angebote"
        assert dept.frame == new_department_frame()
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.department_id == dept.id
        assert not (await draft_status(db, agent)).dirty

        card = await builder_dto(
            db, proposal, steps, may_create=True, read_only=False, show_mission=True
        )
        assert card.department_id == str(dept.id) and card.department_name == "Vertrieb"

        run_row = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="manual",
            state="running",
            agent_version_id=agent.current_version_id,
            context={},
        )
        db.add(run_row)
        await db.flush()
        pinned = await resolve_version(db, run_row, agent)
        assert "odoo" in pinned["narrowing"]["tools"]
        assert "odoo" in {c.name for c in await _connections(db, run_row, dept)}


async def test_a_member_who_may_not_create_departments_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        seat = await _seat(db, tenant, "operator")
        with pytest.raises(BuilderInvalid) as refused:
            await _propose_as(
                db, office, seat, flat_operations(_sales_agent(office)), role="operator"
            )
        assert refused.value.code == "department_create_not_allowed"
        assert (await db.execute(select(CopilotProposal))).scalars().all() == []


async def test_builder_options_tell_an_employee_to_pick_an_existing_department(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        member = await db.get(m.OrgMember, office.employee.member_id)
        assert member is not None
        from oc8.authz.scope import AgentActor

        actor = AgentActor(
            member=member, scope=await scope_for_member(db, member, token_role="member")
        )
        text = await builder_options_text(db, tenant_id=tenant, actor=actor, token_role="member")
        assert "New department: not allowed" in text


async def test_a_department_name_that_exists_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        seat = await _seat(db, tenant, "manager")
        ops = flat_operations({**_sales_agent(office), "newDepartment": {"name": "buchhaltung"}})
        with pytest.raises(BuilderInvalid) as refused:
            await _propose_as(db, office, seat, ops, role="dept_manager")
        assert refused.value.code == "department_exists"


async def test_the_new_department_and_a_department_id_are_not_both_given(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        with pytest.raises(BuilderInvalid) as refused:
            flat_operations({**_sales_agent(office), "departmentId": str(office.department_id)})
        assert refused.value.code == "two_departments"


async def test_a_failing_step_leaves_no_department_behind(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        seat = await _seat(db, tenant, "manager")
        proposal = await _propose_as(
            db,
            office,
            seat,
            flat_operations(_sales_agent(office, schedule="0 8 * * *")),
            role="dept_manager",
        )
        # The model is withdrawn after proposing: re-checked on apply, refused.
        model = await db.get(m.ModelConfig, office.model_id)
        assert model is not None
        model.chat_enabled = False
        await db.flush()
        with pytest.raises(BuilderInvalid):
            await apply_builder_proposal(
                db,
                proposal_id=proposal.id,
                expect_status="draft",
                applied_by=seat.subject,
                principal=None,
                now=NOW,
            )
        assert (
            await db.execute(select(m.Department).where(m.Department.name == "Vertrieb"))
        ).scalar_one_or_none() is None
        assert (
            await db.execute(select(m.Agent).where(m.Agent.name == "Angebotsassistent"))
        ).scalar_one_or_none() is None


async def _requested_new_department(
    db: AsyncSession, office: BuilderOffice
) -> tuple[CopilotProposal, m.ApprovalRequest]:
    """Proposed by a department manager and sent for approval -- to nobody's
    department, since it does not exist yet."""
    seat = await _seat(db, office.tenant, "manager")
    proposal = await _propose_as(
        db, office, seat, flat_operations(_sales_agent(office)), role="dept_manager"
    )
    member = await db.get(m.OrgMember, seat.member_id)
    assert member is not None
    approval = await request_builder_approval(
        db,
        proposal_id=proposal.id,
        requester=member,
        scope=await scope_for_member(db, member, token_role="dept_manager"),
        tenant_wide=True,
        now=NOW,
    )
    assert approval.department_id is None
    return proposal, approval


async def test_an_approver_must_be_allowed_to_create_departments(
    app_session: AppSessionFactory,
) -> None:
    """Checked again as the approver when it is applied. No built-in role that
    may decide it lacks the right today; the check holds for whatever role
    comes next."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested_new_department(db, office)
        seat = await _seat(db, tenant, "operator")
        actor = await _human(db, office, seat, "operator")
        with pytest.raises(EffectRefused) as refused:
            await resolve_builder_approval(
                db, approval=approval, decision="approve", reason=None, actor=actor
            )
        assert "department_create_not_allowed" in str(refused.value)
        assert proposal.status == "awaiting_approval"
        assert (
            await db.execute(select(m.Department).where(m.Department.name == "Vertrieb"))
        ).scalar_one_or_none() is None


async def test_an_approver_who_may_create_departments_creates_both(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested_new_department(db, office)
        actor = await _human(db, office, office.admin, "org_admin")
        await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=actor)
        assert proposal.status == "applied"
        agent = await db.get(m.Agent, uuid.UUID(proposal.result["agentId"]))
        assert agent is not None
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None and dept.name == "Vertrieb"


async def test_only_a_leading_department_the_agent_goes_into_is_accepted() -> None:
    agent = {"type": "agent.create", "ref": "$new", "name": "A", "departmentRef": "$d"}
    dept = {"type": "department.create", "ref": "$d", "name": "Vertrieb"}
    steps = parse_builder_operations([dept, agent])
    assert [builder_operation_data(s) for s in steps] == [
        {
            "type": "department.create",
            "ref": "$d",
            "name": "Vertrieb",
            "goal": "",
            "icon": "building",
        },
        {
            "type": "agent.create",
            "ref": "$new",
            "name": "A",
            "roleTitle": "",
            "mission": "",
            "departmentRef": "$d",
        },
    ]
    created = {"$d": uuid.uuid4()}
    data, ref = resolve_step(builder_operation_data(steps[1]), created)
    assert ref == "$new" and data["departmentId"] == str(created["$d"])

    for ops, code in [
        ([agent, dept], "type_not_allowed"),
        ([dept, {**agent, "departmentRef": "$other"}], "unknown_department_ref"),
        ([dept, {**agent, "departmentId": str(uuid.uuid4())}], "unknown_department_ref"),
        ([{**agent, "departmentId": str(uuid.uuid4())}], "unknown_department_ref"),
        ([{**dept, "ref": None}, agent], "missing_ref"),
    ]:
        with pytest.raises(BuilderInvalid) as refused:
            parse_builder_operations(ops)
        assert refused.value.code == code, ops
    with pytest.raises(BuilderInvalid):
        resolve_step(builder_operation_data(steps[1]), {})
