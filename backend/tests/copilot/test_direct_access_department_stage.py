"""A direct write a department's own policy holds asks the PERSON first, then
the department (spec §1: every write is the person's; a policy only adds a
gate). Two stages, two cards: the person's, then -- only after they approved
-- the department's. The run wakes, and the write runs, only on both."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.approvals import (
    NotYourDepartment,
    NotYourSayAtAll,
    decide_approval,
    member_direct_access_cards,
)
from oc8.approvals.repo import awaiting_department, direct_access_owner
from oc8.auth.principal import Principal
from oc8.authz.permissions import MEMBER_ROLE, SEAT_APPROVER
from oc8.authz.scope import AgentActor, HumanActor, scope_for_member, scope_for_principal
from oc8.chat.service import delete_session
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import DirectOffice, chat_run, direct_office

pytestmark = pytest.mark.asyncio


async def _human(db: AsyncSession, tenant: uuid.UUID, subject: str, role: str) -> HumanActor:
    principal = Principal(subject=subject, tenant_id=tenant, role=role)
    member, scope = await scope_for_principal(db, principal, upsert=True)
    assert member is not None
    return HumanActor(principal=principal, member=member, scope=scope)


async def _approver(db: AsyncSession, office: DirectOffice, subject: str = "hos") -> HumanActor:
    """An approver of the department whose policy holds the write."""
    actor = await _human(db, office.tenant, subject, MEMBER_ROLE)
    db.add(
        m.OrgMemberDepartment(
            tenant_id=office.tenant,
            member_id=actor.member.id,
            department_id=office.department_id,
            seat_role=SEAT_APPROVER,
        )
    )
    await db.flush()
    return await _human(db, office.tenant, subject, MEMBER_ROLE)


async def _member_stage(
    db: AsyncSession, office: DirectOffice, run: m.AgentRun
) -> m.ApprovalRequest:
    """The card the engine raises for a department-held direct write: the
    person's, naming the department that decides after them."""
    held = m.ApprovalRequest(
        tenant_id=office.tenant,
        agent_id=office.copilot_id,
        task_id=office.task_id,
        department_id=None,
        action_type="tool_send",
        title="Copilot on behalf of Anna: change things (update_record)",
        detail="needs approval",
        status="pending",
        payload={
            "tool": "update_record",
            "arguments": {"id": 7},
            "justification": "",
            "preview": "",
            "read_before_write_ok": True,
            "gate_connection": "things",
            "run_id": str(run.id),
            "i18n": {"de": {"title": "Copilot im Auftrag von Anna: things ändern"}},
            "direct_access": {
                "member_id": str(office.member_id),
                "member_name": "Anna",
                "session_id": str(office.session_id),
                "tool_key": "things",
                "label": "things",
                "account": "organisation",
                "decider": "member",
                "department_id": str(office.department_id),
            },
        },
    )
    db.add(held)
    await db.flush()
    return held


async def _tool_sends(db: AsyncSession) -> list[m.ApprovalRequest]:
    return list(
        (
            await db.execute(
                select(m.ApprovalRequest)
                .where(m.ApprovalRequest.action_type == "tool_send")
                .order_by(m.ApprovalRequest.created_at, m.ApprovalRequest.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _department_stage(db: AsyncSession, member_card: m.ApprovalRequest) -> m.ApprovalRequest:
    (row,) = [r for r in await _tool_sends(db) if r.id != member_card.id]
    return row


async def _parked(db: AsyncSession, office: DirectOffice) -> tuple[m.AgentRun, m.ApprovalRequest]:
    run = await chat_run(db, office, state="waiting_for_approval")
    task = await db.get(m.Task, office.task_id)
    assert task is not None
    task.state = "waiting_for_approval"
    await db.flush()
    return run, await _member_stage(db, office, run)


async def test_the_person_approves_first_and_the_department_is_asked_next(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run, card = await _parked(db, office)
        assert direct_access_owner(card) == office.member_id
        anna = await _human(db, tenant, "anna", "member")
        result = await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=anna)
        # The run is NOT woken and nothing is recorded on it yet.
        assert result.resumed_run_id is None
        await db.refresh(run)
        assert run.state == "waiting_for_approval"
        assert not (run.context or {}).get("resolved_tool_approvals")
        task = await db.get(m.Task, office.task_id, populate_existing=True)
        assert task is not None and task.state == "waiting_for_approval"
        dept = await _department_stage(db, card)
        assert dept.status == "pending"
        assert dept.department_id == office.department_id
        assert direct_access_owner(dept) is None
        info = dept.payload["direct_access"]
        assert info["decider"] == "department"
        assert info["member_approval_id"] == str(card.id)
        assert info["member_id"] == str(office.member_id)
        assert info["session_id"] == str(office.session_id)
        assert dept.payload["run_id"] == str(run.id)
        assert dept.payload["tool"] == "update_record"
        assert dept.payload["arguments"] == {"id": 7}
        assert dept.payload["read_before_write_ok"] is True
        assert dept.payload["gate_connection"] == "things"
        assert dept.title == card.title
        assert dept.payload["i18n"]["de"]["title"] == card.payload["i18n"]["de"]["title"]
        assert dept.task_id == card.task_id and dept.agent_id == card.agent_id


async def test_both_approvals_wake_the_run_with_one_approved_call(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run, card = await _parked(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=anna)
        dept = await _department_stage(db, card)
        hos = await _approver(db, office)
        result = await decide_approval(db, dept, decision="approve", tenant_id=tenant, actor=hos)
        assert result.resumed_run_id == run.id
        await db.refresh(run)
        (entry,) = run.context["resolved_tool_approvals"]
        assert entry["decision"] == "approve" and entry["tool"] == "update_record"
        # The proof the person's card carried travels to the one that resumes.
        assert entry["read_before_write_ok"] is True


async def test_the_person_rejecting_never_reaches_the_department(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run, card = await _parked(db, office)
        anna = await _human(db, tenant, "anna", "member")
        result = await decide_approval(db, card, decision="reject", tenant_id=tenant, actor=anna)
        assert result.resumed_run_id == run.id
        assert [r.id for r in await _tool_sends(db)] == [card.id]
        await db.refresh(run)
        (entry,) = run.context["resolved_tool_approvals"]
        assert entry["decision"] == "reject"


async def test_the_department_rejecting_refuses_the_call(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run, card = await _parked(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=anna)
        hos = await _approver(db, office)
        dept = await _department_stage(db, card)
        result = await decide_approval(db, dept, decision="reject", tenant_id=tenant, actor=hos)
        assert result.resumed_run_id == run.id
        await db.refresh(run)
        (entry,) = run.context["resolved_tool_approvals"]
        assert entry["decision"] == "reject"


async def test_a_department_approver_cannot_decide_the_persons_stage(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        _run, card = await _parked(db, office)
        hos = await _approver(db, office)
        with pytest.raises(NotYourDepartment):
            await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=hos)
        assert [r.id for r in await _tool_sends(db)] == [card.id]


async def test_the_person_alone_cannot_decide_the_departments_stage(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Anna holds only a viewer seat: her approval never stands in for the
    department's."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run, card = await _parked(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=anna)
        dept = await _department_stage(db, card)
        with pytest.raises(NotYourDepartment):
            await decide_approval(db, dept, decision="approve", tenant_id=tenant, actor=anna)
        await db.refresh(run)
        assert not (run.context or {}).get("resolved_tool_approvals")


async def test_the_copilot_decides_neither_stage(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Even when the person is an approver of the holding department, the
    Copilot acting as them decides neither card."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, seated=False)
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=office.member_id,
                department_id=office.department_id,
                seat_role=SEAT_APPROVER,
            )
        )
        await db.flush()
        run, card = await _parked(db, office)
        member = await db.get(m.OrgMember, office.member_id)
        assert member is not None
        copilot = AgentActor(member=member, scope=await scope_for_member(db, member))
        with pytest.raises(NotYourSayAtAll):
            await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=copilot)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=anna)
        dept = await _department_stage(db, card)
        assert copilot.scope.may_decide(dept.department_id)
        with pytest.raises(NotYourSayAtAll):
            await decide_approval(db, dept, decision="approve", tenant_id=tenant, actor=copilot)
        await db.refresh(dept)
        assert dept.status == "pending"
        await db.refresh(run)
        assert not (run.context or {}).get("resolved_tool_approvals")


async def test_the_persons_listing_shows_the_card_waiting_for_the_department(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        _run, card = await _parked(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=anna)
        dept = await _department_stage(db, card)
        rows = await member_direct_access_cards(db, member_id=office.member_id)
        # Her own card, approved, and never the department's card itself.
        assert [r.id for r in rows] == [card.id]
        assert await awaiting_department(db, [r.id for r in rows]) == {card.id}
        hos = await _approver(db, office)
        await decide_approval(db, dept, decision="approve", tenant_id=tenant, actor=hos)
        assert await member_direct_access_cards(db, member_id=office.member_id) == []
        assert await awaiting_department(db, [card.id]) == set()


async def test_deleting_the_chat_expires_the_department_stage(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        _run, card = await _parked(db, office)
        anna = await _human(db, tenant, "anna", "member")
        await decide_approval(db, card, decision="approve", tenant_id=tenant, actor=anna)
        dept = await _department_stage(db, card)
        session = await db.get(m.ChatSession, office.session_id)
        assert session is not None
        await delete_session(db, tenant_id=tenant, session=session)
        await db.flush()
        await db.refresh(dept)
        assert dept.status == "expired"
        assert await awaiting_department(db, [card.id]) == set()


async def test_deleting_the_chat_expires_the_persons_stage(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        _run, card = await _parked(db, office)
        session = await db.get(m.ChatSession, office.session_id)
        assert session is not None
        await delete_session(db, tenant_id=tenant, session=session)
        await db.flush()
        await db.refresh(card)
        assert card.status == "expired"
        assert [r.id for r in await _tool_sends(db)] == [card.id]
