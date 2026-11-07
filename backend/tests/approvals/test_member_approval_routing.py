"""An approval a member's own guardrail asked for goes to that member first (D3a).

The member's rule is theirs: nobody else -- not the department's approver, not
an administrator who sees every department -- answers it in their name. When
the department ALSO requires approval, the member's yes opens a second stage
in the department's queue, and only that second yes resumes the run. A member
who is also an approver of that department answers both stages with one click,
and the trail records both.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.approvals import NotYourApproval, decide_approval, raise_approval
from oc8.approvals.repo import load_for_actor, visible_approvals
from oc8.approvals.service import approval_routing
from oc8.auth.principal import Principal
from oc8.authz.pdp import Decision, Effect
from oc8.authz.permissions import ORG_ADMIN, SEAT_APPROVER
from oc8.authz.scope import AgentActor, HumanActor, scope_for_principal, subject_uuid_for
from oc8.channels import binding as channel_binding
from oc8.channels.dispatch import decision_from
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

PAYLOAD: dict[str, Any] = {"tool": "erp.confirm_order", "arguments": {"order": "SO-1"}}


@dataclass
class Office:
    sales: uuid.UUID
    agent: uuid.UUID
    task: uuid.UUID
    run: uuid.UUID
    anna: m.OrgMember
    dana: m.OrgMember
    olaf: m.OrgMember


def _member(tenant: uuid.UUID, subject: str, **kw: Any) -> m.OrgMember:
    return m.OrgMember(
        tenant_id=tenant, subject=subject, subject_uuid=subject_uuid_for(subject), **kw
    )


async def _office(db: Any, tenant: uuid.UUID, *, anna_approves_too: bool = False) -> Office:
    sales = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add(sales)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=sales.id,
        name="Nora",
        status="waiting_for_approval",
        narrowing={},
        definition={},
    )
    anna = _member(tenant, "anna", display_name="Anna")
    dana = _member(tenant, "dana", display_name="Dana")
    olaf = _member(tenant, "olaf", display_name="Olaf", all_departments=True)
    db.add_all([agent, anna, dana, olaf])
    await db.flush()
    seats = [dana] + ([anna] if anna_approves_too else [])
    for seated in seats:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=seated.id,
                department_id=sales.id,
                seat_role=SEAT_APPROVER,
            )
        )
    task = m.Task(tenant_id=tenant, department_id=sales.id, title="t", state="waiting_for_approval")
    db.add(task)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        state=RunState.WAITING_FOR_APPROVAL.value,
        context={"task": "original"},
    )
    db.add(run)
    await db.flush()
    return Office(sales.id, agent.id, task.id, run.id, anna, dana, olaf)


async def _actor(db: Any, tenant: uuid.UUID, member: m.OrgMember, role: str) -> HumanActor:
    principal = Principal(subject=member.subject, tenant_id=tenant, role=role)
    resolved, scope = await scope_for_principal(db, principal)
    assert resolved is not None
    return HumanActor(principal=principal, member=resolved, scope=scope)


async def _raise_member_stage(
    db: Any, tenant: uuid.UUID, office: Office, *, department_follows: bool
) -> m.ApprovalRequest:
    payload = {**PAYLOAD, **({"department_stage": True} if department_follows else {})}
    return await raise_approval(
        db,
        tenant_id=tenant,
        agent_id=office.agent,
        task_id=office.task,
        action_type="tool_send",
        title="Nora wants to call erp.confirm_order",
        detail="your own rule asks you",
        amount_text="12.480,00 EUR",
        payload=payload,
        reason_code="condition_matched",
        reason_context={"limit": 5000},
        assignee_member_id=office.anna.id,
    )


async def _run_state(db: Any, run_id: uuid.UUID) -> str:
    run: m.AgentRun | None = await db.get(m.AgentRun, run_id, populate_existing=True)
    assert run is not None
    return run.state


async def _rows(db: Any, task_id: uuid.UUID) -> list[m.ApprovalRequest]:
    return list(
        (
            await db.execute(
                select(m.ApprovalRequest)
                .where(m.ApprovalRequest.task_id == task_id)
                .order_by(m.ApprovalRequest.created_at, m.ApprovalRequest.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


# ------------------------------------------------------------------ routing


async def test_routing_follows_who_the_decision_asked() -> None:
    anna = m.OrgMember(id=uuid.uuid4(), subject="anna")

    def asked(*who: str) -> Decision:
        return Decision(Effect.REQUIRE_APPROVAL, approval_from=frozenset(who))

    assert approval_routing(asked("member"), anna) == (anna.id, False)
    assert approval_routing(asked("member", "department"), anna) == (anna.id, True)
    assert approval_routing(asked("department"), anna) == (None, False)
    assert approval_routing(asked("member"), None) == (None, False)


# ------------------------------------------------------------ member alone


async def test_the_member_approves_their_own_stage_and_the_run_resumes(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        approval = await _raise_member_stage(db, tenant, office, department_follows=False)
        anna = await _actor(db, tenant, office.anna, "member")

        result = await decide_approval(
            db, approval, decision="approve", tenant_id=tenant, actor=anna
        )

        assert result.resumed_run_id == office.run
        assert result.next_stage_id is None
        assert await _run_state(db, office.run) == RunState.QUEUED.value
        rows = await _rows(db, office.task)
        assert [r.id for r in rows] == [approval.id]
        assert rows[0].decided_by == office.anna.id


async def test_nobody_else_decides_the_members_stage(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        approval = await _raise_member_stage(db, tenant, office, department_follows=False)
        dana = await _actor(db, tenant, office.dana, "member")
        olaf = await _actor(db, tenant, office.olaf, ORG_ADMIN)

        for other in (dana, olaf):
            with pytest.raises(NotYourApproval):
                await decide_approval(
                    db, approval, decision="approve", tenant_id=tenant, actor=other
                )

        # Not even the member themself through the Copilot door: an assistant
        # deciding the guardrail its own user set is the guardrail deciding
        # nothing.
        copilot = AgentActor(member=office.anna, scope=dana.scope)
        with pytest.raises(NotYourApproval):
            await decide_approval(db, approval, decision="approve", tenant_id=tenant, actor=copilot)

        row = (await _rows(db, office.task))[0]
        assert row.status == "pending"
        assert row.decided_by is None
        assert await _run_state(db, office.run) == RunState.WAITING_FOR_APPROVAL.value


# --------------------------------------------------------------- two stages


async def test_the_members_yes_opens_the_department_stage(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        first = await _raise_member_stage(db, tenant, office, department_follows=True)
        anna = await _actor(db, tenant, office.anna, "member")
        dana = await _actor(db, tenant, office.dana, "member")

        result = await decide_approval(db, first, decision="approve", tenant_id=tenant, actor=anna)

        assert result.resumed_run_id is None
        assert result.next_stage_id is not None
        assert await _run_state(db, office.run) == RunState.WAITING_FOR_APPROVAL.value
        run = await db.get(m.AgentRun, office.run)
        assert run is not None
        assert not (run.context or {}).get("resolved_tool_approvals"), (
            "the member's yes alone must not pre-decide the held call"
        )

        rows = await _rows(db, office.task)
        assert len(rows) == 2
        second = next(r for r in rows if r.id == result.next_stage_id)
        assert second.status == "pending"
        assert second.previous_approval_id == first.id
        assert second.assignee_member_id is None
        assert second.department_id == office.sales
        assert {k: v for k, v in second.payload.items() if k != "channel_handles"} == PAYLOAD
        assert second.action_type == "tool_send"
        assert second.title == first.title
        assert second.amount_text == first.amount_text
        assert second.reason_context == first.reason_context

        final = await decide_approval(db, second, decision="approve", tenant_id=tenant, actor=dana)
        assert final.resumed_run_id == office.run
        assert await _run_state(db, office.run) == RunState.QUEUED.value


async def test_the_department_rejecting_stage_two_rejects_the_run(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        first = await _raise_member_stage(db, tenant, office, department_follows=True)
        anna = await _actor(db, tenant, office.anna, "member")
        dana = await _actor(db, tenant, office.dana, "member")

        result = await decide_approval(db, first, decision="approve", tenant_id=tenant, actor=anna)
        second = next(r for r in await _rows(db, office.task) if r.id == result.next_stage_id)
        final = await decide_approval(db, second, decision="reject", tenant_id=tenant, actor=dana)

        assert final.resumed_run_id == office.run
        run = await db.get(m.AgentRun, office.run, populate_existing=True)
        assert run is not None
        entries = run.context["resolved_tool_approvals"]
        assert [e["decision"] for e in entries] == ["reject"]


async def test_the_member_rejecting_stage_one_rejects_the_run(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        first = await _raise_member_stage(db, tenant, office, department_follows=True)
        anna = await _actor(db, tenant, office.anna, "member")

        result = await decide_approval(db, first, decision="reject", tenant_id=tenant, actor=anna)

        assert result.resumed_run_id == office.run
        assert result.next_stage_id is None
        assert len(await _rows(db, office.task)) == 1
        run = await db.get(m.AgentRun, office.run, populate_existing=True)
        assert run is not None
        assert [e["decision"] for e in run.context["resolved_tool_approvals"]] == ["reject"]


async def test_a_member_who_also_approves_for_the_department_clicks_once(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant, anna_approves_too=True)
        first = await _raise_member_stage(db, tenant, office, department_follows=True)
        anna = await _actor(db, tenant, office.anna, "member")

        result = await decide_approval(db, first, decision="approve", tenant_id=tenant, actor=anna)

        assert result.resumed_run_id == office.run
        assert result.next_stage_id is not None
        rows = await _rows(db, office.task)
        assert len(rows) == 2
        second = next(r for r in rows if r.id == result.next_stage_id)
        assert second.previous_approval_id == first.id
        assert second.status == "approved"
        assert second.decided_by == office.anna.id
        assert second.decided_at is not None

        events = (
            (await db.execute(select(m.AuditEvent).where(m.AuditEvent.category == "approval")))
            .scalars()
            .all()
        )
        assert sorted(e.resource["approval_id"] for e in events) == sorted(
            [str(first.id), str(second.id)]
        )
        assert all(e.actor_id == office.anna.id for e in events)


# --------------------------------------------------------------- visibility


async def test_only_the_member_sees_their_stage(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        first = await _raise_member_stage(db, tenant, office, department_follows=True)
        anna = await _actor(db, tenant, office.anna, "member")
        dana = await _actor(db, tenant, office.dana, "member")
        olaf = await _actor(db, tenant, office.olaf, ORG_ADMIN)

        async def pending(actor: HumanActor) -> list[uuid.UUID]:
            rows = await visible_approvals(db, actor=actor, status="pending")
            return [r.id for r in rows]

        assert await pending(anna) == [first.id]
        assert await pending(dana) == []
        assert await pending(olaf) == [], "an unrestricted admin is not the member"
        assert await load_for_actor(db, first.id, actor=anna) is not None
        assert await load_for_actor(db, first.id, actor=dana) is None
        assert await load_for_actor(db, first.id, actor=olaf) is None
        assert (
            await visible_approvals(db, actor=anna, status="pending", department_id=office.sales)
            == []
        ), "a department filter is a filter over the department queue"

        result = await decide_approval(db, first, decision="approve", tenant_id=tenant, actor=anna)
        assert result.next_stage_id is not None

        assert await pending(dana) == [result.next_stage_id]
        assert await pending(olaf) == [result.next_stage_id]
        assert await pending(anna) == [], "Anna holds no seat in Sales"


# ---------------------------------------------------------- messenger door


async def test_the_member_is_told_and_answers_on_their_phone(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        for who, chat in ((office.anna, "chat-anna"), (office.dana, "chat-dana")):
            db.add(
                m.ApprovalChannelBinding(
                    tenant_id=tenant,
                    channel="fake",
                    user_id=who.subject_uuid,
                    external_id=chat,
                    member_id=who.id,
                )
            )
        await db.flush()

        told = await channel_binding.recipients(
            db,
            tenant_id=tenant,
            channel="fake",
            department_id=office.sales,
            assignee_member_id=office.anna.id,
        )
        assert [b.external_id for b in told] == ["chat-anna"]

        first = await _raise_member_stage(db, tenant, office, department_follows=False)
        approval_id = first.id

    async with app_session(tenant) as db:
        with pytest.raises(PermissionError):
            await decision_from(
                db,
                tenant_id=tenant,
                channel_id="fake",
                external_id="chat-dana",
                approval_id=approval_id,
                verdict="approve",
            )

    async with app_session(tenant) as db:
        result = await decision_from(
            db,
            tenant_id=tenant,
            channel_id="fake",
            external_id="chat-anna",
            approval_id=approval_id,
            verdict="approve",
        )
        assert result.approval.decided_by == office.anna.id
        assert result.resumed_run_id == office.run
