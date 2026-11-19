"""§3 steps 3-6: only the conversation's member decides; approving writes the
grant and wakes the run; writes resume their own run."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.v1._serializers import approval_to_dto
from oc8.approvals import (
    AgentNotOffered,
    AgentNotRunnable,
    AlreadyDecided,
    EffectRefused,
    NotYourDepartment,
    NotYourSayAtAll,
    UnknownOption,
    decide_approval,
    direct_access_owner,
    expire_approval,
)
from oc8.approvals.repo import load_for_actor, visible_approvals
from oc8.auth.principal import Principal
from oc8.authz.permissions import COPILOT_USE, MEMBER_ROLE, RUN_START, SEAT_APPROVER
from oc8.authz.scope import (
    AgentActor,
    HumanActor,
    scope_for_member,
    scope_for_principal,
    subject_uuid_for,
)
from oc8.copilot.direct_access import (
    _declined_in_this_run,
    direct_access_resume_text,
    execute_direct_access_tool,
    resolve_direct_access_approval,
)
from oc8.modelrouter import ToolCall
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import (
    DirectOffice,
    approved_offer,
    chat_run,
    direct_office,
)


async def _human(db: AsyncSession, tenant: uuid.UUID, subject: str, role: str) -> HumanActor:
    principal = Principal(subject=subject, tenant_id=tenant, role=role)
    member, scope = await scope_for_principal(db, principal, upsert=True)
    assert member is not None
    return HumanActor(principal=principal, member=member, scope=scope)


async def _copilot_department(db: AsyncSession, office: DirectOffice) -> uuid.UUID:
    copilot = await db.get(m.Agent, office.copilot_id)
    assert copilot is not None
    return copilot.department_id


async def _copilot_approver(db: AsyncSession, office: DirectOffice) -> HumanActor:
    """A Head of the Copilot's own department: may decide everything filed there."""
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
            department_id=await _copilot_department(db, office),
            seat_role=SEAT_APPROVER,
        )
    )
    await db.flush()
    return await _human(db, office.tenant, "hos", MEMBER_ROLE)


async def _parked_offer(
    db: AsyncSession, office: DirectOffice
) -> tuple[m.ApprovalRequest, m.AgentRun]:
    run = await chat_run(db, office, state="waiting_for_approval")
    offer = await approved_offer(db, office, status="pending")
    offer.payload = {
        **offer.payload,
        "run_id": str(run.id),
        "tool": "offer_direct_access",
        "arguments": {"connection": "things", "purpose": "p"},
    }
    await db.flush()
    return offer, run


async def _grants(db: AsyncSession) -> list[m.DirectAccessGrant]:
    return list((await db.execute(select(m.DirectAccessGrant))).scalars())


async def test_the_member_approves_and_the_run_wakes_with_the_grant(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, run = await _parked_offer(db, office)
        anna = await _human(db, tenant, "anna", "member")
        result = await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=anna)
        assert result.resumed_run_id == run.id
        (row,) = await _grants(db)
        assert (row.chat_session_id, row.tool_key, row.member_id, row.approval_id) == (
            office.session_id,
            "things",
            office.member_id,
            offer.id,
        )
        await db.refresh(run)
        assert run.state == RunState.QUEUED.value
        assert run.context["resolved_direct_access"]["decision"] == "approve"


async def test_nobody_else_decides_even_an_admin(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run = await _parked_offer(db, office)
        admin = await _human(db, tenant, "boss", "org_admin")
        with pytest.raises(NotYourDepartment):
            await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=admin)
        assert await _grants(db) == []


async def test_a_department_approver_of_the_copilot_cannot_decide_an_offer(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """R21: filed under the Copilot's department or not, the person's own
    request is not that department's to answer."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run = await _parked_offer(db, office)
        offer.department_id = await _copilot_department(db, office)
        await db.flush()
        hos = await _copilot_approver(db, office)
        assert hos.scope.may_decide(offer.department_id)
        with pytest.raises(NotYourDepartment):
            await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=hos)
        assert await _grants(db) == []
        await db.refresh(offer)
        assert offer.status == "pending"


async def test_the_copilot_cannot_approve_its_own_request(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run = await _parked_offer(db, office)
        member = await db.get(m.OrgMember, office.member_id)
        assert member is not None
        copilot = AgentActor(member=member, scope=await scope_for_member(db, member))
        with pytest.raises(NotYourSayAtAll):
            await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=copilot)
        assert await _grants(db) == []


async def test_a_rejection_writes_no_grant_and_tells_the_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, run = await _parked_offer(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, offer, decision="reject", tenant_id=tenant, actor=anna)
        assert await _grants(db) == []
        await db.refresh(run)
        assert run.context["resolved_direct_access"]["decision"] == "reject"


async def test_a_decline_stays_declined_after_another_answer_in_the_same_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """`resolved_direct_access` holds the latest answer; a decline of an
    earlier key must not be forgotten by it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, run = await _parked_offer(db, office)
        await resolve_direct_access_approval(db, approval=offer, decision="reject")
        other = await approved_offer(db, office, tool_key="other", status="pending")
        other.payload = {**other.payload, "run_id": str(run.id)}
        await db.flush()
        await resolve_direct_access_approval(db, approval=other, decision="reject")
        await db.refresh(run)
        assert _declined_in_this_run(run.context, "things")
        assert _declined_in_this_run(run.context, "other")


async def test_a_second_decision_is_refused_and_one_grant_exists(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run = await _parked_offer(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=anna)
        with pytest.raises(AlreadyDecided):
            await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=anna)
        assert len(await _grants(db)) == 1


async def test_an_expired_offer_grants_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run = await _parked_offer(db, office)
        await expire_approval(db, offer)
        anna = await _human(db, tenant, "anna", "member")
        with pytest.raises(AlreadyDecided):
            await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=anna)
        assert await _grants(db) == []


async def test_approving_for_a_member_who_lost_access_is_refused(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, seated=False)
        offer, _run = await _parked_offer(db, office)
        anna = await _human(db, tenant, "anna", "member")
        with pytest.raises(EffectRefused):
            await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=anna)


async def _held_write(
    db: AsyncSession, office: DirectOffice, run: m.AgentRun, *, decider: str
) -> m.ApprovalRequest:
    held = m.ApprovalRequest(
        tenant_id=office.tenant,
        agent_id=office.copilot_id,
        task_id=office.task_id,
        department_id=office.department_id if decider == "department" else None,
        action_type="tool_send",
        title="Copilot on behalf of Anna wants to call update_record",
        status="pending",
        payload={
            "tool": "update_record",
            "arguments": {"id": 7},
            "run_id": str(run.id),
            "direct_access": {
                "member_id": str(office.member_id),
                "member_name": "Anna",
                "session_id": str(office.session_id),
                "tool_key": "things",
                "label": "things",
                "account": "organisation",
                "decider": decider,
                "department_id": str(office.department_id) if decider == "department" else None,
            },
        },
    )
    db.add(held)
    await db.flush()
    return held


async def test_a_direct_write_resumes_its_own_run_among_several(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Regression: two runs on the conversation's one Task used to make the
    task-keyed lookup raise MultipleResultsFound."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await chat_run(db, office, state="done")
        parked = await chat_run(db, office, state="waiting_for_approval")
        held = await _held_write(db, office, parked, decider="member")
        anna = await _human(db, tenant, "anna", "member")
        result = await decide_approval(db, held, decision="approve", tenant_id=tenant, actor=anna)
        assert result.resumed_run_id == parked.id


async def test_a_department_held_write_is_the_departments_to_decide(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)  # Anna holds only a viewer seat
        parked = await chat_run(db, office, state="waiting_for_approval")
        held = await _held_write(db, office, parked, decider="department")
        assert direct_access_owner(held) is None
        anna = await _human(db, tenant, "anna", "member")
        with pytest.raises(NotYourDepartment):
            await decide_approval(db, held, decision="approve", tenant_id=tenant, actor=anna)


async def test_an_offer_is_raised_in_no_department(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """R21: the person's card is not filed in the Copilot's department queue."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run = await chat_run(db, office)
        agent = await db.get(m.Agent, office.copilot_id)
        task = await db.get(m.Task, office.task_id)
        assert agent is not None and task is not None
        await execute_direct_access_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(
                id="c",
                name="offer_direct_access",
                arguments={"connection": "things", "purpose": "find the quotation"},
            ),
            run_id=run.id,
        )
        offer = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "direct_access")
            )
        ).scalar_one()
        assert offer.department_id is None
        assert direct_access_owner(offer) == office.member_id


async def test_only_the_owner_lists_or_loads_an_owned_approval(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """R21: a department approver -- even of the department the card happens
    to be filed in -- and an admin neither list nor load the person's card
    (title and purpose are theirs). The person does, from any seat."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, run = await _parked_offer(db, office)
        offer.department_id = await _copilot_department(db, office)
        await db.flush()
        loose = await _held_write(db, office, run, decider="member")  # department_id NULL
        held = await _held_write(db, office, run, decider="department")
        hos = await _copilot_approver(db, office)
        admin = await _human(db, tenant, "boss", "org_admin")
        anna = await _human(db, tenant, "anna", "member")

        def ids(rows: list[m.ApprovalRequest]) -> set[uuid.UUID]:
            return {r.id for r in rows}

        assert ids(await visible_approvals(db, actor=hos, status="pending")) == set()
        assert held.id in ids(await visible_approvals(db, actor=admin, status="pending"))
        assert ids(await visible_approvals(db, actor=admin, status="pending")).isdisjoint(
            {offer.id, loose.id}
        )
        assert {offer.id, loose.id} <= ids(
            await visible_approvals(db, actor=anna, status="pending")
        )
        assert await load_for_actor(db, offer.id, actor=hos) is None
        assert await load_for_actor(db, loose.id, actor=admin) is None
        assert await load_for_actor(db, offer.id, actor=anna) is not None
        assert await load_for_actor(db, loose.id, actor=anna) is not None


async def test_the_dto_says_who_and_which_account(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        parked = await chat_run(db, office, state="waiting_for_approval")
        held = await _held_write(db, office, parked, decider="member")
        held.payload = {
            **held.payload,
            "i18n": {"de": {"title": "Copilot im Auftrag von Anna: update_record"}},
        }
        dto = approval_to_dto(held)
        assert dto.run_id == str(parked.id)
        assert dto.direct_access is not None
        assert (dto.direct_access.member_name, dto.direct_access.account) == (
            "Anna",
            "organisation",
        )
        assert dto.direct_access.decider == "member"
        assert dto.title_translations == {"de": "Copilot im Auftrag von Anna: update_record"}


def test_the_resumed_run_is_told_the_outcome() -> None:
    yes: dict[str, Any] = {"tool_key": "things", "label": "Things", "decision": "approve"}
    no: dict[str, Any] = {"tool_key": "things", "label": "Things", "decision": "reject"}
    assert direct_access_resume_text("find it", yes).startswith("find it")
    assert "approved" in direct_access_resume_text("find it", yes)
    assert "declined" in direct_access_resume_text("find it", no)


# ---------------------------------------------- Copilot: existing agent first


async def _may_run(db: AsyncSession, office: DirectOffice) -> m.Role:
    """Anna's own role: she may use the Copilot and start runs where she sits."""
    role = m.Role(tenant_id=office.tenant, name=f"r-{uuid.uuid4().hex[:8]}", kind="human")
    db.add(role)
    await db.flush()
    for permission in (COPILOT_USE, RUN_START):
        db.add(m.RolePermission(tenant_id=office.tenant, role_id=role.id, permission=permission))
    member = await db.get(m.OrgMember, office.member_id)
    assert member is not None
    member.role_id = role.id
    await db.flush()
    return role


async def _offer_with_agent(
    db: AsyncSession, office: DirectOffice
) -> tuple[m.ApprovalRequest, m.AgentRun, m.Agent]:
    """Anna may run the Quote Writer in Sales; her parked offer names it."""
    await _may_run(db, office)
    writer = m.Agent(
        tenant_id=office.tenant,
        department_id=office.department_id,
        name="Quote Writer",
        role_title="",
        mission="Writes quotations",
        narrowing={},
        status="idle",
    )
    db.add(writer)
    await db.flush()
    offer, run = await _parked_offer(db, office)
    offer.payload = {
        **offer.payload,
        "direct_access": {
            **offer.payload["direct_access"],
            "purpose": "find the quotation",
            "request": "Draft the quotation for Miller",
            "login_name": "things",
            "agents": [
                {"id": str(writer.id), "name": "Quote Writer", "department": "Sales", "score": 0.9}
            ],
        },
    }
    await db.flush()
    return offer, run, writer


async def _delegations(db: AsyncSession) -> list[m.AgentRun]:
    return list(
        (await db.execute(select(m.AgentRun).where(m.AgentRun.source == "delegation"))).scalars()
    )


async def test_choosing_an_agent_starts_it_as_a_delegation_and_grants_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, run, writer = await _offer_with_agent(db, office)
        anna = await _human(db, tenant, "anna", "member")
        result = await decide_approval(
            db, offer, decision="approve", tenant_id=tenant, actor=anna, agent_id=writer.id
        )
        assert await _grants(db) == []
        (sub,) = await _delegations(db)
        assert result.started_run_id == sub.id and result.resumed_run_id == run.id
        assert sub.agent_id == writer.id and sub.state == RunState.QUEUED.value
        # The Copilot's delegation: its task is woken with the result, in the
        # person's conversation.
        assert sub.context["parent_task_id"] == str(office.task_id)
        assert sub.context["chat_session_id"] == str(office.session_id)
        assert "Draft the quotation for Miller" in sub.context["task"]
        assert "find the quotation" in sub.context["task"]
        assert offer.status == "approved"
        assert offer.payload["direct_access"]["chosen_agent"] == {
            "id": str(writer.id),
            "name": "Quote Writer",
            "run_id": str(sub.id),
        }
        await db.refresh(run)
        assert run.state == RunState.QUEUED.value
        told = direct_access_resume_text("find it", run.context["resolved_direct_access"])
        assert told.endswith(
            f"[oc8] The person chose to run Quote Writer instead (run {sub.id}). Wait for "
            "its result; do not do the task yourself."
        )
        offer_id, sub_id, writer_id = offer.id, sub.id, writer.id
        await db.commit()  # an audit event is written to the chain on commit
    async with app_session(tenant) as db:
        (event,) = (
            await db.execute(
                select(m.AuditEvent).where(m.AuditEvent.action == "direct_access.agent_chosen")
            )
        ).scalars()
        assert event.resource == {
            "approval_id": str(offer_id),
            "chat_session_id": str(office.session_id),
            "agent_id": str(writer_id),
            "run_id": str(sub_id),
        }


@pytest.mark.parametrize("change", ["agent_paused", "role_reduced", "agent_pending"])
async def test_choosing_an_agent_the_person_may_no_longer_run_is_refused(
    app_session: AppSessionFactory, redis_url: str, change: str
) -> None:
    """Review Focus 1: the run right is checked again at the click."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run, writer = await _offer_with_agent(db, office)
        if change == "agent_paused":
            writer.status = "paused"
        elif change == "agent_pending":
            writer.status = "pending_approval"
        else:
            member = await db.get(m.OrgMember, office.member_id)
            assert member is not None and member.role_id is not None
            (grant_row,) = (
                await db.execute(
                    select(m.RolePermission).where(
                        m.RolePermission.role_id == member.role_id,
                        m.RolePermission.permission == RUN_START,
                    )
                )
            ).scalars()
            await db.delete(grant_row)
        await db.flush()
        anna = await _human(db, tenant, "anna", "member")
        with pytest.raises(AgentNotRunnable) as refused:
            await decide_approval(
                db, offer, decision="approve", tenant_id=tenant, actor=anna, agent_id=writer.id
            )
        assert refused.value.code == "agent_not_runnable"
        assert refused.value.agent_name == "Quote Writer"
        assert await _delegations(db) == [] and await _grants(db) == []


async def test_the_run_right_is_judged_with_the_role_on_the_copilot_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """No assigned role: the claim recorded on the Copilot run that made the
    offer decides, as on every turn that acts for the person."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, run, writer = await _offer_with_agent(db, office)
        member = await db.get(m.OrgMember, office.member_id)
        assert member is not None
        member.role_id = None
        run.context = {**run.context, "operator_role": "operator"}
        await db.flush()
        anna = await _human(db, tenant, "anna", "member")
        result = await decide_approval(
            db, offer, decision="approve", tenant_id=tenant, actor=anna, agent_id=writer.id
        )
        assert result.started_run_id is not None


async def test_choosing_an_agent_not_on_the_card_is_refused(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run, _writer = await _offer_with_agent(db, office)
        other = m.Agent(
            tenant_id=tenant,
            department_id=office.department_id,
            name="Other",
            role_title="",
            mission="",
            narrowing={},
            status="idle",
        )
        db.add(other)
        await db.flush()
        anna = await _human(db, tenant, "anna", "member")
        with pytest.raises(AgentNotOffered):
            await decide_approval(
                db, offer, decision="approve", tenant_id=tenant, actor=anna, agent_id=other.id
            )
        assert await _delegations(db) == [] and await _grants(db) == []


async def test_a_decided_offer_cannot_start_a_second_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Review Focus 5: a second click -- with the agent again, or for the
    direct grant -- is the same 409 as any decided card."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run, writer = await _offer_with_agent(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(
            db, offer, decision="approve", tenant_id=tenant, actor=anna, agent_id=writer.id
        )
        with pytest.raises(AlreadyDecided):
            await decide_approval(
                db, offer, decision="approve", tenant_id=tenant, actor=anna, agent_id=writer.id
            )
        with pytest.raises(AlreadyDecided):
            await decide_approval(db, offer, decision="approve", tenant_id=tenant, actor=anna)
        assert len(await _delegations(db)) == 1 and await _grants(db) == []


async def test_a_chosen_agent_only_answers_a_direct_access_offer(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        parked = await chat_run(db, office, state="waiting_for_approval")
        held = await _held_write(db, office, parked, decider="member")
        anna = await _human(db, tenant, "anna", "member")
        with pytest.raises(UnknownOption):
            await decide_approval(
                db, held, decision="approve", tenant_id=tenant, actor=anna, agent_id=uuid.uuid4()
            )


async def test_the_dto_carries_agents_login_and_chosen_agent(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        offer, _run, writer = await _offer_with_agent(db, office)
        dto = approval_to_dto(offer)
        assert dto.direct_access is not None
        assert [(a.id, a.name, a.department, a.score) for a in dto.direct_access.agents] == [
            (str(writer.id), "Quote Writer", "Sales", 0.9)
        ]
        assert dto.direct_access.login_name == "things"
        assert dto.direct_access.chosen_agent is None
        anna = await _human(db, tenant, "anna", "member")
        result = await decide_approval(
            db, offer, decision="approve", tenant_id=tenant, actor=anna, agent_id=writer.id
        )
        chosen = approval_to_dto(result.approval).direct_access
        assert chosen is not None and chosen.chosen_agent is not None
        assert (chosen.chosen_agent.id, chosen.chosen_agent.name, chosen.chosen_agent.run_id) == (
            str(writer.id),
            "Quote Writer",
            str(result.started_run_id),
        )
