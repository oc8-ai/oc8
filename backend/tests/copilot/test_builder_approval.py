"""Decision B (spec §3.4): a member who may not create agents in a department
sends the proposal for approval; whoever may create agents there decides, and
approving applies it with the approver's authority."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.audit.helpers import flush_audit
from oc8 import models as m
from oc8.approvals import (
    AlreadyDecided,
    EffectRefused,
    NotYourDepartment,
    NotYourSayAtAll,
    decide_approval,
)
from oc8.auth import Principal
from oc8.authz.authority import authority_for_member, tenant_wide_read
from oc8.authz.permissions import DEPARTMENT, SEAT_APPROVER, VIEW, perm
from oc8.authz.scope import DepartmentScope, HumanActor, scope_for_member
from oc8.copilot.builder import create_builder_proposal, request_builder_approval
from oc8.copilot.builder_ops import BuilderInvalid
from oc8.copilot.models import CopilotProposal
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import BuilderOffice, builder_office, invoice_checker_ops
from tests.copilot.helpers import Seat, copilot_seat

pytestmark = pytest.mark.asyncio
NOW = dt.datetime.now(tz=dt.UTC)


class _Recorder:
    def __init__(self) -> None:
        self.member_events: list[tuple[list[uuid.UUID], str, dict[str, Any]]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish_member_event(  # type: ignore[no-untyped-def]
        self, tenant_id, member_ids, type_, data, *, source
    ):
        self.member_events.append((list(member_ids), type_, data))

    async def publish_event(  # type: ignore[no-untyped-def]
        self, tenant_id, type_, data, *, source
    ):
        self.events.append((type_, data))


async def _load[T](db: AsyncSession, model: type[T], row_id: uuid.UUID) -> T:
    row = await db.get(model, row_id)
    assert row is not None
    return row


async def _reach(db: AsyncSession, member: m.OrgMember) -> tuple[DepartmentScope, bool]:
    scope = await scope_for_member(db, member, token_role="member")
    authority = await authority_for_member(db, member, token_role="member")
    return scope, tenant_wide_read(authority, perm(DEPARTMENT, VIEW))


async def _propose(
    db: AsyncSession, office: BuilderOffice, seat: Seat | None = None
) -> CopilotProposal:
    seat = seat or office.employee
    member = await _load(db, m.OrgMember, seat.member_id)
    scope, tenant_wide = await _reach(db, member)
    return await create_builder_proposal(
        db,
        tenant_id=office.tenant,
        member=member,
        scope=scope,
        tenant_wide=tenant_wide,
        session_id=seat.session_id,
        operations=invoice_checker_ops(office),
        now=NOW,
    )


async def _request(
    db: AsyncSession,
    proposal_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    now: dt.datetime = NOW,
) -> m.ApprovalRequest:
    member = await _load(db, m.OrgMember, member_id)
    scope, tenant_wide = await _reach(db, member)
    return await request_builder_approval(
        db,
        proposal_id=proposal_id,
        requester=member,
        scope=scope,
        tenant_wide=tenant_wide,
        now=now,
    )


async def _requested(
    db: AsyncSession, office: BuilderOffice, seat: Seat | None = None
) -> tuple[CopilotProposal, m.ApprovalRequest]:
    seat = seat or office.employee
    proposal = await _propose(db, office, seat)
    approval = await _request(db, proposal.id, seat.member_id)
    return proposal, approval


async def _human(
    db: AsyncSession, office: BuilderOffice, member_id: uuid.UUID, subject: str
) -> HumanActor:
    member = await db.get(m.OrgMember, member_id)
    assert member is not None
    return HumanActor(
        principal=Principal(subject=subject, tenant_id=office.tenant, role="member"),
        member=member,
        scope=await scope_for_member(db, member, token_role="member"),
    )


async def _agents_named(db: AsyncSession, name: str = "Rechnungsprüfer") -> list[m.Agent]:
    return list((await db.execute(select(m.Agent).where(m.Agent.name == name))).scalars().all())


async def test_a_request_lands_in_the_target_departments_queue(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office)
        assert approval.action_type == "copilot.agent_builder"
        assert approval.department_id == office.department_id
        assert approval.agent_id == office.assistant_id
        assert approval.payload["proposal_id"] == str(proposal.id)
        assert approval.expires_at == proposal.expires_at
        assert proposal.status == "awaiting_approval"
        assert proposal.result["approvalId"] == str(approval.id)
        # No mission text anywhere on the approval (it goes to a queue and a
        # messenger), and the request is audited with both ids (Ruling A13).
        mission = invoice_checker_ops(office)[0]["mission"]
        assert mission not in f"{approval.title} {approval.detail} {approval.payload}"
        await flush_audit(db)
        audit = (
            await db.execute(
                select(m.AuditEvent).where(m.AuditEvent.action == "agent_builder.requested")
            )
        ).scalar_one()
        assert audit.actor_id == office.employee.member_id
        assert audit.resource["requested_by"] == "emp"
        assert audit.resource["requested_by_member_id"] == str(office.employee.member_id)
        assert mission not in str(audit.resource)


async def test_only_the_owner_may_request(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office)
        with pytest.raises(BuilderInvalid) as caught:
            await _request(db, proposal.id, office.lead.member_id)
        assert caught.value.code == "not_found"
        assert proposal.status == "draft"


async def test_a_request_awaiting_approval_is_not_requested_twice(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, _approval = await _requested(db, office)
        with pytest.raises(BuilderInvalid) as caught:
            await _request(db, proposal.id, office.employee.member_id)
        assert caught.value.code == "not_open"
        count = (
            await db.execute(
                select(m.ApprovalRequest).where(
                    m.ApprovalRequest.action_type == "copilot.agent_builder"
                )
            )
        ).all()
        assert len(count) == 1


async def test_an_expired_draft_cannot_be_requested(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office)
        assert proposal.expires_at is not None
        with pytest.raises(BuilderInvalid) as caught:
            await _request(
                db,
                proposal.id,
                office.employee.member_id,
                now=proposal.expires_at + dt.timedelta(seconds=1),
            )
        assert caught.value.code == "expired"
        assert proposal.status == "expired"


async def test_the_lead_approves_and_both_people_are_in_the_audit(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    rec = _Recorder()
    with patch("oc8.realtime.emit.get_event_bus", return_value=rec):
        async with app_session(tenant) as db:
            office = await builder_office(db, tenant)
            proposal, approval = await _requested(db, office)
            lead = await _human(db, office, office.lead.member_id, "lead")
            await decide_approval(
                db, approval, decision="approve", tenant_id=tenant, actor=lead, reason=None
            )
            assert proposal.status == "applied" and proposal.applied_by == "lead"
            agent = await db.get(m.Agent, uuid.UUID(proposal.result["agentId"]))
            assert agent is not None and agent.department_id == office.department_id
            await flush_audit(db)
            audit = (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "agent_builder.applied")
                )
            ).scalar_one()
            assert audit.actor_id == office.lead.member_id
            assert audit.resource["requested_by"] == "emp"
            assert audit.resource["requested_by_member_id"] == str(office.employee.member_id)
            assert audit.resource["applied_by"] == "lead"
            assert audit.resource["applied_by_member_id"] == str(office.lead.member_id)
            assert audit.resource["approval_id"] == str(approval.id)
            # Nothing is published until the transaction commits (Ruling A9).
            assert not [e for e in rec.member_events if e[1] == "copilot.agent_builder"]
        await asyncio.sleep(0.05)
    told = [e for e in rec.member_events if e[1] == "copilot.agent_builder"]
    assert told and all(e[0] == [office.employee.member_id] for e in told)
    assert told[-1][2] == {
        "proposal_id": str(proposal.id),
        "session_id": str(office.employee.session_id),
    }


async def test_a_rolled_back_decision_tells_nobody(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    rec = _Recorder()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        _proposal, approval = await _requested(db, office)
        aid = approval.id
    with patch("oc8.realtime.emit.get_event_bus", return_value=rec):
        async with app_session(tenant) as db:
            approval = await _load(db, m.ApprovalRequest, aid)
            assert approval is not None
            lead = await _human(db, office, office.lead.member_id, "lead")
            await decide_approval(db, approval, decision="reject", tenant_id=tenant, actor=lead)
            await db.rollback()
        await asyncio.sleep(0.05)
    assert not [e for e in rec.member_events if e[1] == "copilot.agent_builder"]


async def test_an_approver_without_the_agent_toggle_may_not_approve(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        _proposal, approval = await _requested(db, office)
        seat = await copilot_seat(db, tenant, "approver-only")
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=seat.member_id,
                department_id=office.department_id,
                seat_role=SEAT_APPROVER,
                agent_manage=False,
            )
        )
        await db.flush()
        actor = await _human(db, office, seat.member_id, "approver-only")
        with pytest.raises(NotYourSayAtAll):
            await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=actor)
        assert approval.status == "pending"


async def test_a_requester_with_a_viewer_seat_cannot_decide(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        _proposal, approval = await _requested(db, office)
        emp = await _human(db, office, office.employee.member_id, "emp")
        # A viewer seat: not even in the department's queue.
        with pytest.raises(NotYourDepartment):
            await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=emp)
        assert approval.status == "pending"


async def test_a_requester_who_may_approve_may_approve_their_own_request(
    app_session: AppSessionFactory,
) -> None:
    """Self-approval is allowed, and harmless: approving needs exactly the right
    to create the agent there directly, which this requester already holds."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office, office.lead)
        lead = await _human(db, office, office.lead.member_id, "lead")
        await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=lead)
        assert approval.status == "approved"
        assert proposal.status == "applied" and proposal.applied_by == "lead"
        assert len(await _agents_named(db)) == 1


async def test_a_requester_who_lost_the_department_cannot_request(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office)
        seats = (
            await db.execute(
                select(m.OrgMemberDepartment).where(
                    m.OrgMemberDepartment.member_id == office.employee.member_id
                )
            )
        ).scalars()
        for seat in seats:
            await db.delete(seat)
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _request(db, proposal.id, office.employee.member_id)
        assert caught.value.code == "department_unknown"
        assert proposal.status == "draft"


async def test_a_rejection_closes_the_approval_even_when_the_proposal_is_gone(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        _proposal, approval = await _requested(db, office)
        approval.payload = {**approval.payload, "proposal_id": str(uuid.uuid4())}
        await db.flush()
        lead = await _human(db, office, office.lead.member_id, "lead")
        await decide_approval(db, approval, decision="reject", tenant_id=tenant, actor=lead)
        assert approval.status == "rejected"


async def test_a_mismatched_request_is_rejected_without_touching_the_proposal(
    app_session: AppSessionFactory,
) -> None:
    """An approval naming a proposal that waits on ANOTHER request: approving it
    is refused (and leaves it pending); rejecting it closes it and leaves the
    proposal waiting on its own request."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, _approval = await _requested(db, office)
        stale = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=office.assistant_id,
            department_id=office.department_id,
            action_type="copilot.agent_builder",
            title="Create agent: old",
            detail="",
            payload={"proposal_id": str(proposal.id)},
            status="pending",
        )
        db.add(stale)
        await db.flush()
        pid, sid = proposal.id, stale.id
    with pytest.raises(EffectRefused) as caught:
        async with app_session(tenant) as db:
            lead = await _human(db, office, office.lead.member_id, "lead")
            row = await _load(db, m.ApprovalRequest, sid)
            await decide_approval(db, row, decision="approve", tenant_id=tenant, actor=lead)
    assert caught.value.code == "not_open"
    async with app_session(tenant) as db:
        assert (await _load(db, m.ApprovalRequest, sid)).status == "pending"
        lead = await _human(db, office, office.lead.member_id, "lead")
        row = await _load(db, m.ApprovalRequest, sid)
        await decide_approval(db, row, decision="reject", tenant_id=tenant, actor=lead)
        assert row.status == "rejected"
        untouched = await _load(db, CopilotProposal, pid)
        assert untouched.status == "awaiting_approval"
        assert "rejectedReason" not in untouched.result
        assert await _agents_named(db) == []


async def test_proposing_again_does_not_lock_a_request_awaiting_approval(
    app_session: AppSessionFactory,
) -> None:
    """Lock order: the funnel locks an awaiting proposal from the approval side,
    so a new propose in the same chat only READS it (and is refused)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, _approval = await _requested(db, office)
        pid = proposal.id
    async with app_session(tenant) as holder:
        from oc8.copilot.builder import lock_builder_proposal

        assert await lock_builder_proposal(holder, pid) is not None
        async with app_session(tenant) as db:
            with pytest.raises(BuilderInvalid) as caught:
                await asyncio.wait_for(_propose(db, office), timeout=5)
            assert caught.value.code == "awaiting_approval"


async def test_proposing_again_after_the_request_expired_closes_the_request(
    app_session: AppSessionFactory,
) -> None:
    """A due request is expired by the next propose in that chat; its approval
    must not stay pending in the queue for a proposal that can never apply."""
    tenant = uuid.uuid4()
    rec = _Recorder()
    with (
        patch("oc8.realtime.bus.get_event_bus", return_value=rec),
        patch("oc8.realtime.emit.get_event_bus", return_value=rec),
    ):
        async with app_session(tenant) as db:
            office = await builder_office(db, tenant)
            proposal, approval = await _requested(db, office)
            proposal.expires_at = NOW - dt.timedelta(minutes=1)
            await db.flush()
            fresh = await _propose(db, office)
            assert fresh.status == "draft" and proposal.status == "expired"
            assert approval.status == "expired" and approval.decided_at is not None
            assert approval.decided_by is None, "nobody decided it"
            assert not rec.events, "nothing is announced before the commit"
        await asyncio.sleep(0.05)
    assert ("approval.decided", {"approval_id": str(approval.id), "status": "expired"}) in (
        rec.events
    )
    told = {e[2]["proposal_id"] for e in rec.member_events if e[1] == "copilot.agent_builder"}
    assert str(proposal.id) in told


async def test_a_rejection_keeps_its_reason(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office)
        lead = await _human(db, office, office.lead.member_id, "lead")
        await decide_approval(
            db, approval, decision="reject", tenant_id=tenant, actor=lead, reason="Zu teuer"
        )
        assert proposal.status == "rejected"
        assert proposal.result["rejectedReason"] == "Zu teuer"
        assert await _agents_named(db) == []


async def test_an_expired_request_can_still_be_rejected_but_not_approved(
    app_session: AppSessionFactory,
) -> None:
    """Ruling A5: there is no approval sweeper, so an expired request must be
    clearable; approving it stays refused and leaves it pending."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office)
        proposal.expires_at = NOW - dt.timedelta(minutes=1)
        pid, aid = proposal.id, approval.id
    with pytest.raises(EffectRefused) as caught:
        async with app_session(tenant) as db:
            approval = await _load(db, m.ApprovalRequest, aid)
            assert approval is not None
            lead = await _human(db, office, office.lead.member_id, "lead")
            await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=lead)
    assert caught.value.code == "expired"
    async with app_session(tenant) as db:
        approval = await _load(db, m.ApprovalRequest, aid)
        assert approval is not None and approval.status == "pending"
        # A read in between expires the proposal for good; rejecting still works.
        proposal = await _load(db, CopilotProposal, pid)
        assert proposal is not None
        proposal.status = "expired"
    async with app_session(tenant) as db:
        approval = await _load(db, m.ApprovalRequest, aid)
        assert approval is not None
        lead = await _human(db, office, office.lead.member_id, "lead")
        await decide_approval(
            db, approval, decision="reject", tenant_id=tenant, actor=lead, reason="zu spät"
        )
        proposal = await _load(db, CopilotProposal, pid)
        assert proposal is not None and proposal.status == "rejected"
        assert approval.status == "rejected"
        assert await _agents_named(db) == []


async def test_a_refused_apply_leaves_the_approval_pending_and_creates_nothing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office)
        pid, aid = proposal.id, approval.id
    async with app_session(tenant) as db:
        # A colleague took the name meanwhile.
        db.add(
            m.Agent(tenant_id=tenant, department_id=office.department_id, name="Rechnungsprüfer")
        )
    with pytest.raises(EffectRefused) as caught:
        async with app_session(tenant) as db:
            approval = await _load(db, m.ApprovalRequest, aid)
            assert approval is not None
            lead = await _human(db, office, office.lead.member_id, "lead")
            await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=lead)
    assert caught.value.code == "name_taken"
    assert str(caught.value) == "agent_builder:name_taken"
    async with app_session(tenant) as db:
        approval = await _load(db, m.ApprovalRequest, aid)
        proposal = await _load(db, CopilotProposal, pid)
        assert approval is not None and approval.status == "pending"
        assert proposal is not None and proposal.status == "awaiting_approval"
        assert len(await _agents_named(db)) == 1, "only the colleague's"


async def test_approving_under_the_hire_gate_raises_the_hire_request(
    app_session: AppSessionFactory,
) -> None:
    """Ruling A11 holds on the approval path: the approver's apply raises the
    tenant's hire request in the same transaction."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:6]}",
                name="T",
                tier="standard",
                region="eu",
                settings={"require_hire_approval": True},
            )
        )
        await db.flush()
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office)
        lead = await _human(db, office, office.lead.member_id, "lead")
        await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=lead)
        agent = await db.get(m.Agent, uuid.UUID(proposal.result["agentId"]))
        assert agent is not None and agent.status == "pending_approval"
        hire = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "hire_agent")
            )
        ).scalar_one()
        assert hire.agent_id == agent.id


async def _decide_in_own_session(
    app_session: AppSessionFactory,
    office: BuilderOffice,
    aid: uuid.UUID,
    decision: str,
    started: asyncio.Event,
    release: asyncio.Event,
) -> str:
    try:
        async with app_session(office.tenant) as db:
            approval = await _load(db, m.ApprovalRequest, aid)
            assert approval is not None
            lead = await _human(db, office, office.lead.member_id, "lead")
            started.set()
            await release.wait()
            await decide_approval(
                db, approval, decision=decision, tenant_id=office.tenant, actor=lead
            )
            # Hold the transaction open a moment so the other one has to wait.
            await asyncio.sleep(0.2)
        return "decided"
    except (AlreadyDecided, EffectRefused) as exc:
        return type(exc).__name__


@pytest.mark.parametrize("second", ["approve", "reject"])
async def test_racing_decisions_have_one_outcome(
    app_session: AppSessionFactory, second: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office)
        pid, aid = proposal.id, approval.id
    release = asyncio.Event()
    started = [asyncio.Event(), asyncio.Event()]
    tasks = [
        asyncio.create_task(
            _decide_in_own_session(app_session, office, aid, "approve", started[0], release)
        ),
        asyncio.create_task(
            _decide_in_own_session(app_session, office, aid, second, started[1], release)
        ),
    ]
    await asyncio.gather(*(s.wait() for s in started))
    release.set()
    outcomes = sorted(await asyncio.gather(*tasks))
    assert outcomes == ["AlreadyDecided", "decided"]
    async with app_session(tenant) as db:
        proposal_row = await db.get(CopilotProposal, pid)
        approval_row = await db.get(m.ApprovalRequest, aid)
        assert proposal_row is not None and approval_row is not None
        agents = await _agents_named(db)
        if approval_row.status == "approved":
            assert proposal_row.status == "applied" and len(agents) == 1
        else:
            assert approval_row.status == "rejected"
            assert proposal_row.status == "rejected" and agents == []


async def test_the_approval_dto_names_the_proposal(app_session: AppSessionFactory) -> None:
    from oc8.api.v1._serializers import approval_to_dto

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal, approval = await _requested(db, office)
        dto = approval_to_dto(approval)
        assert dto.proposal_id == str(proposal.id)
        assert dto.title_translations["de"].startswith("Agent anlegen: ")
        other = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=office.assistant_id,
            action_type="decision",
            title="x",
            detail="",
            payload={"proposal_id": str(proposal.id)},
            status="pending",
        )
        assert approval_to_dto(other).proposal_id is None
