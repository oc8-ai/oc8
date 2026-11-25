"""Always allow (for me): a person's rule answers their own next write card
-- and only that, re-checked every time (constraints: own card, write tier,
may still decide; never the department's stage)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.approvals import decide_approval
from oc8.approvals.allow_rules import (
    NotEligible,
    auto_decide_by_rules,
    create_rule_from_approval,
    member_rules,
    revoke_rule,
    rule_eligible,
)
from oc8.auth.principal import Principal
from oc8.authz.scope import HumanActor, scope_for_principal
from oc8.modelrouter import ToolCall
from oc8.runtime.executor import _auto_decide_by_rules
from tests.agent.test_engine_direct_access import _copilot, _held, _patch, _run
from tests.agent.test_engine_parallel_reads import _RecordingServer, _turn
from tests.audit.helpers import flush_audit
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import DirectOffice, chat_run, direct_office, grant

pytestmark = pytest.mark.asyncio


async def _anna(db: AsyncSession, office: DirectOffice) -> HumanActor:
    principal = Principal(subject=office.subject, tenant_id=office.tenant, role="member")
    member, scope = await scope_for_principal(db, principal, upsert=True)
    assert member is not None and member.id == office.member_id
    return HumanActor(principal=principal, member=member, scope=scope)


async def _card(
    db: AsyncSession,
    office: DirectOffice,
    run: m.AgentRun,
    *,
    tier: str | None = "write",
    connection_id: uuid.UUID | str | None = "office",
    department: bool = False,
    member_id: uuid.UUID | None = None,
    status: str = "pending",
) -> m.ApprovalRequest:
    """The person's card the engine raises for a direct write."""
    conn = office.connection_id if connection_id == "office" else connection_id
    payload: dict[str, Any] = {
        "tool": "update_record",
        "arguments": {"id": 7},
        "run_id": str(run.id),
        "connection_id": str(conn) if conn else None,
        "direct_access": {
            "member_id": str(member_id or office.member_id),
            "member_name": "Anna",
            "session_id": str(office.session_id),
            "tool_key": "things",
            "label": "things",
            "account": "organisation",
            "decider": "member",
            "department_id": str(office.department_id) if department else None,
        },
    }
    if tier is not None:
        payload["risk_tier"] = tier
    row = m.ApprovalRequest(
        tenant_id=office.tenant,
        agent_id=office.copilot_id,
        task_id=office.task_id,
        department_id=None,
        action_type="tool_send",
        title="Copilot on behalf of Anna: change things (update_record)",
        status=status,
        payload=payload,
    )
    db.add(row)
    await db.flush()
    return row


async def _parked(db: AsyncSession, office: DirectOffice, **ctx: Any) -> m.AgentRun:
    run = await chat_run(db, office, state="waiting_for_approval", extra=ctx or None)
    task = await db.get(m.Task, office.task_id)
    assert task is not None
    task.state = "waiting_for_approval"
    await db.flush()
    return run


async def _rule(db: AsyncSession, office: DirectOffice) -> m.ToolAllowRule:
    """Anna approves her card with "always allow"."""
    first_run = await _parked(db, office)
    first = await _card(db, office, first_run)
    await decide_approval(
        db, first, decision="approve", tenant_id=office.tenant, actor=await _anna(db, office)
    )
    rule = await create_rule_from_approval(db, approval=first, member_id=office.member_id)
    # That run moved on; the next card is a new run's.
    first_run.state = "done"
    await db.flush()
    return rule


async def _fresh(db: AsyncSession, row: Any) -> Any:
    await db.refresh(row)
    return row


async def test_a_matching_rule_decides_the_next_card_and_keeps_it_for_audit(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        rule = await _rule(db, office)
        run = await _parked(db, office)
        card = await _card(db, office, run)

        resumed = await auto_decide_by_rules(db, run=run)

        assert resumed == [run.id]
        card = await _fresh(db, card)
        assert card.status == "approved"
        assert card.decided_by == office.member_id
        assert card.reason == f"always-allow rule {rule.id}"
        run = await _fresh(db, run)
        assert run.state == "queued"
        (entry,) = run.context["resolved_tool_approvals"]
        assert entry["decision"] == "approve" and entry["tool"] == "update_record"


async def test_the_park_path_requeues_and_survives_a_failing_rule_check(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor's call: a rule wakes the run; any failure leaves the card
    pending and the run parked, and never fails the park."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _rule(db, office)
        run = await _parked(db, office)
        card = await _card(db, office, run)

        async def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("decide failed")

        monkeypatch.setattr("oc8.approvals.allow_rules.decide_approval", _boom)
        assert await _auto_decide_by_rules(db, run) == []
        assert (await _fresh(db, card)).status == "pending"
        assert run.state == "waiting_for_approval"

        monkeypatch.undo()
        assert await _auto_decide_by_rules(db, run) == [run.id]
        assert run.state == "queued"


async def test_a_rule_never_skips_the_department_stage(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _rule(db, office)
        run = await _parked(db, office)
        card = await _card(db, office, run, department=True)

        assert await auto_decide_by_rules(db, run=run) == []

        assert (await _fresh(db, card)).status == "approved"
        stages = [
            r
            for r in (
                await db.execute(
                    select(m.ApprovalRequest)
                    .where(m.ApprovalRequest.action_type == "tool_send")
                    .execution_options(populate_existing=True)
                )
            ).scalars()
            if (r.payload.get("direct_access") or {}).get("decider") == "department"
        ]
        assert len(stages) == 1 and stages[0].status == "pending"
        # A second look decides nothing either: the stage is not a member card.
        assert await auto_decide_by_rules(db, run=run) == []
        assert (await _fresh(db, stages[0])).status == "pending"
        assert (await _fresh(db, run)).state == "waiting_for_approval"


async def test_a_rule_does_not_apply_for_another_members_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        bob = m.OrgMember(tenant_id=tenant, subject="bob", subject_uuid=uuid.uuid4())
        db.add(bob)
        await db.flush()
        # Bob's own rule for exactly this agent, connection and tool...
        db.add(
            m.ToolAllowRule(
                tenant_id=tenant,
                member_id=bob.id,
                agent_id=office.copilot_id,
                connection_id=office.connection_id,
                tool_key="update_record",
            )
        )
        await db.flush()
        # ...answers nothing of Anna's.
        run = await _parked(db, office)
        card = await _card(db, office, run)
        assert await auto_decide_by_rules(db, run=run) == []
        assert (await _fresh(db, card)).status == "pending"


@pytest.mark.parametrize("change", ["offboarded", "seat_revoked", "role_without_rights"])
async def test_a_rule_is_rechecked_when_the_member_lost_the_right(
    app_session: AppSessionFactory, redis_url: str, change: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _rule(db, office)
        run = await _parked(db, office, operator_role="member")
        card = await _card(db, office, run)
        member = await db.get(m.OrgMember, office.member_id)
        assert member is not None
        if change == "offboarded":
            member.deleted_at = dt.datetime.now(dt.UTC)
        elif change == "seat_revoked":
            run.context = {k: v for k, v in run.context.items() if k != "operator_role"}
            for seat in (
                await db.execute(
                    select(m.OrgMemberDepartment).where(
                        m.OrgMemberDepartment.member_id == office.member_id
                    )
                )
            ).scalars():
                seat.revoked_at = dt.datetime.now(dt.UTC)
        else:
            # The token claim alone would admit her (member holds copilot:use);
            # an assigned role replaces it, and this one grants nothing.
            for seat in (
                await db.execute(
                    select(m.OrgMemberDepartment).where(
                        m.OrgMemberDepartment.member_id == office.member_id
                    )
                )
            ).scalars():
                seat.revoked_at = dt.datetime.now(dt.UTC)
            role = m.Role(tenant_id=tenant, name=f"Nothing {uuid.uuid4().hex[:8]}", kind="human")
            db.add(role)
            await db.flush()
            member.role_id = role.id
        await db.flush()

        assert await auto_decide_by_rules(db, run=run) == []
        assert (await _fresh(db, card)).status == "pending"
        assert (await _fresh(db, run)).state == "waiting_for_approval"


async def test_the_token_claim_of_the_run_admits_an_unseated_member(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The control for the role case above: with no seat, the claim her own
    turn recorded (member: copilot:use) is what lets her rule answer."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _rule(db, office)
        for seat in (
            await db.execute(
                select(m.OrgMemberDepartment).where(
                    m.OrgMemberDepartment.member_id == office.member_id
                )
            )
        ).scalars():
            seat.revoked_at = dt.datetime.now(dt.UTC)
        run = await _parked(db, office, operator_role="member")
        card = await _card(db, office, run)
        assert await auto_decide_by_rules(db, run=run) == [run.id]
        assert (await _fresh(db, card)).status == "approved"


async def test_a_rule_keyed_on_a_deleted_connection_does_not_match_a_new_one_with_the_same_name(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _rule(db, office)
        old = await db.get(m.McpConnection, office.connection_id)
        assert old is not None
        await db.delete(old)
        await db.flush()
        new = m.McpConnection(
            tenant_id=tenant,
            department_id=office.department_id,
            name="things",
            transport="stdio",
            server_url="stdio://things",
            connected=True,
            config={"command": "x", "args": []},
            scopes={},
        )
        db.add(new)
        await db.flush()
        run = await _parked(db, office)
        card = await _card(db, office, run, connection_id=new.id)
        assert await auto_decide_by_rules(db, run=run) == []
        assert (await _fresh(db, card)).status == "pending"


async def test_a_rule_never_covers_a_card_whose_tier_is_not_write(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Re-checked from the card, not trusted from the rule: a card for the
    same tool whose stored tier is raised (or missing) stays pending."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _rule(db, office)
        for tier in ("destructive", "irreversible", "outward", None):
            run = await _parked(db, office)
            card = await _card(db, office, run, tier=tier)
            assert not rule_eligible(card)
            assert await auto_decide_by_rules(db, run=run) == []
            assert (await _fresh(db, card)).status == "pending"
            run.state = "done"
            await db.flush()


async def test_a_rule_is_only_made_from_the_members_own_approved_write(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run = await _parked(db, office)
        pending = await _card(db, office, run)
        with pytest.raises(NotEligible):
            await create_rule_from_approval(db, approval=pending, member_id=office.member_id)
        approved = await _card(db, office, run, status="approved")
        with pytest.raises(NotEligible):
            await create_rule_from_approval(db, approval=approved, member_id=uuid.uuid4())
        for tier in ("destructive", "irreversible", "outward", None):
            card = await _card(db, office, run, tier=tier, status="approved")
            with pytest.raises(NotEligible):
                await create_rule_from_approval(db, approval=card, member_id=office.member_id)
        assert await member_rules(db, member_id=office.member_id) == []
        # Twice for the same terms is one rule.
        one = await create_rule_from_approval(db, approval=approved, member_id=office.member_id)
        two = await create_rule_from_approval(db, approval=approved, member_id=office.member_id)
        assert one.id == two.id
        assert one.connection_id == office.connection_id and one.tool_key == "update_record"


async def test_revoke_stops_matching(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        rule = await _rule(db, office)
        assert await revoke_rule(db, rule_id=rule.id, member_id=office.member_id)
        assert not await revoke_rule(db, rule_id=rule.id, member_id=office.member_id)
        run = await _parked(db, office)
        card = await _card(db, office, run)
        assert await auto_decide_by_rules(db, run=run) == []
        assert (await _fresh(db, card)).status == "pending"
        assert await member_rules(db, member_id=office.member_id) == []


async def test_audit_events_for_create_use_revoke(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        rule = await _rule(db, office)
        run = await _parked(db, office)
        card = await _card(db, office, run)
        await auto_decide_by_rules(db, run=run)
        await revoke_rule(db, rule_id=rule.id, member_id=office.member_id)
        await flush_audit(db)
        events = (
            (
                await db.execute(
                    select(m.AuditEvent)
                    .where(m.AuditEvent.action.like("approval.%"))
                    .order_by(m.AuditEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    by_action = {e.action: e for e in events}
    created = by_action["approval.allow_rule_created"]
    used = by_action["approval.allow_rule_used"]
    revoked = by_action["approval.allow_rule_revoked"]
    for event in (created, used, revoked):
        assert event.resource["rule_id"] == str(rule.id)
        assert event.actor_id == office.member_id
    assert used.resource["approval_id"] == str(card.id)
    assert used.resource["tool_key"] == "update_record"
    # The decision itself is the ordinary one, attributed to the member and
    # saying which door it came through.
    approves = [
        e
        for e in events
        if e.action == "approval.approve" and e.resource["approval_id"] == str(card.id)
    ]
    assert len(approves) == 1 and approves[0].resource["via"] == "always_allow"
    assert approves[0].reason == f"always-allow rule {rule.id}"


async def test_a_parked_write_carries_its_tier_and_connection(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the engine: the held write stores `risk_tier` and
    the connection's id, so the card can become a rule, and the next one is
    answered by it."""
    recorder = _RecordingServer(delay_s=0)
    script = [
        _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "XYZ"})),
        _turn("", ToolCall(id="c2", name="update_record", arguments={"id": 7})),
        _turn("done"),
    ]
    _patch(monkeypatch, recorder, script)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        result = await _run(db, office, agent, run)
        assert result.status == "waiting_for_approval"
        held = await _held(db)
        assert held.payload["risk_tier"] == "write"
        assert held.payload["connection_id"] == str(office.connection_id)
        assert rule_eligible(held)
