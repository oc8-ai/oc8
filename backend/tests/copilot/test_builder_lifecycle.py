"""Builder proposals: one open per chat, checked against what the member may
use, applied in one transaction or not at all, single-use, expiring."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from starlette.requests import Request

from oc8 import models as m
from oc8.api.deps import authorize_agent_write
from oc8.auth import Principal
from oc8.authz.authority import authority_for_member
from oc8.authz.scope import HumanActor, scope_for_member
from oc8.copilot.builder import (
    BUILDER_KIND,
    apply_builder_proposal,
    may_create_agent,
)
from oc8.copilot.builder_ops import BuilderInvalid
from oc8.copilot.capabilities import apply_operation
from oc8.copilot.models import CopilotOperation, CopilotProposal
from oc8.copilot.proposals import ProposalNotRejectable, ProposalRejected, apply_proposal
from oc8.copilot.proposals import reject_proposal as reject_generic
from tests.audit.helpers import flush_audit
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import (
    BuilderOffice,
    builder_office,
    invoice_checker_ops,
    propose_as_admin,
)

pytestmark = pytest.mark.asyncio
NOW = dt.datetime(2026, 11, 9, 9, 0, tzinfo=dt.UTC)


async def _propose(
    db: Any, office: BuilderOffice, ops: list[dict[str, Any]], *, now: dt.datetime = NOW
) -> CopilotProposal:
    return await propose_as_admin(db, office, ops, now=now)


def _admin(office: BuilderOffice) -> Principal:
    return Principal(subject="admin", tenant_id=office.tenant, role="org_admin")


async def _apply(db: Any, office: BuilderOffice, proposal_id: uuid.UUID) -> uuid.UUID:
    return await apply_builder_proposal(
        db,
        proposal_id=proposal_id,
        expect_status="draft",
        applied_by="admin",
        principal=_admin(office),
        now=NOW,
    )


async def test_a_new_proposal_supersedes_the_open_one(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        first = await _propose(db, office, invoice_checker_ops(office))
        second = await _propose(db, office, invoice_checker_ops(office, name="Rechnungsprüferin"))
        assert first.status == "superseded" and second.status == "draft"
        assert second.kind == BUILDER_KIND and second.session_id == office.admin.session_id
        assert second.expires_at == NOW + dt.timedelta(days=7)


async def test_superseding_announces_the_replaced_proposal_after_commit(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    published = AsyncMock()
    monkeypatch.setattr("oc8.realtime.emit.publish_builder_changed", published)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        first = await _propose(db, office, invoice_checker_ops(office))
        second = await _propose(db, office, invoice_checker_ops(office, name="Rechnungsprüferin"))
        published.assert_not_called()  # nothing before the commit
        first_id, second_id = first.id, second.id
    await asyncio.sleep(0.05)
    ids = {c.kwargs["proposal_id"] for c in published.await_args_list}
    assert first_id in ids and second_id not in ids


async def test_a_proposal_awaiting_approval_is_not_superseded(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        first = await _propose(db, office, invoice_checker_ops(office))
        first.status = "awaiting_approval"
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _propose(db, office, invoice_checker_ops(office, name="Rechnungsprüferin"))
        assert caught.value.code == "awaiting_approval"
        assert first.status == "awaiting_approval"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({1: {"modelConfigId": "disabled"}}, "model_not_allowed"),
        ({2: {"skillVersionId": "old"}}, "skill_not_allowed"),
        ({0: {"mission": "Nutze password=hunter2 für Odoo."}}, "secret_value"),
    ],
)
async def test_selections_the_member_may_not_use_are_refused(
    app_session: AppSessionFactory, patch: dict[int, dict[str, str]], code: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        ops = invoice_checker_ops(office)
        for index, fields in patch.items():
            for key, value in fields.items():
                ops[index][key] = {
                    "disabled": str(office.disabled_model_id),
                    "old": str(office.old_skill_version_id),
                }.get(value, value)
        with pytest.raises(BuilderInvalid) as caught:
            await _propose(db, office, ops)
        assert caught.value.code == code


@pytest.mark.parametrize(
    "step",
    [
        {
            "type": "trigger.create",
            "agentRef": "$new",
            "kind": "event",
            "taskText": "Melde neue Rechnungen.",
            "eventSource": "token=abc123",
            "eventType": "invoice.created",
        },
        {
            "type": "trigger.create",
            "agentRef": "$new",
            "kind": "event",
            "taskText": "Melde neue Rechnungen.",
            "eventSource": "odoo",
            "eventType": "Bearer abc.def",
        },
        {
            "type": "agent.narrowing.set",
            "agentRef": "$new",
            "narrowing": {"tools": {"odoo": {"note": "api_key=sk-123"}}},
        },
        {
            "type": "agent.narrowing.set",
            "agentRef": "$new",
            "narrowing": {"tools": {"odoo": {"password": "x"}}},
        },
        {
            "type": "agent.guardrail.set",
            "agentRef": "$new",
            "connectionName": "odoo",
            "function": "create_invoice",
            "decision": "with_limits",
            "conditions": [
                {
                    "attribute": "amount",
                    "datatype": "number",
                    "operator": "lt",
                    "value": {"api_key": "sk-1"},
                    "then": "approval_required",
                }
            ],
        },
    ],
)
async def test_a_credential_in_any_stored_field_is_refused(
    app_session: AppSessionFactory, step: dict[str, Any]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        with pytest.raises(BuilderInvalid) as caught:
            await _propose(db, office, [*invoice_checker_ops(office), step])
        assert caught.value.code == "secret_value" and caught.value.step == 4


async def test_a_connection_outside_the_department_frame_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        ops = [
            *invoice_checker_ops(office),
            {
                "type": "agent.guardrail.set",
                "agentRef": "$new",
                "connectionName": "hubspot",
                "function": "create_deal",
                "decision": "approval_required",
            },
        ]
        with pytest.raises(BuilderInvalid) as caught:
            await _propose(db, office, ops)
        assert caught.value.code == "connection_not_allowed" and caught.value.step == 4


def _narrow_to_odoo() -> dict[str, Any]:
    return {
        "type": "agent.narrowing.set",
        "agentRef": "$new",
        "narrowing": {"tools": {"odoo": {"read": True}}},
    }


async def test_a_framed_system_without_a_usable_connection_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """Ruling A12: the frame enabling a system is not enough -- a connected
    connection of that name must exist, tenant-wide or in this department."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        conn = (
            await db.execute(select(m.McpConnection).where(m.McpConnection.tenant_id == tenant))
        ).scalar_one()
        conn.department_id = office.other_department_id
        await db.flush()
        with pytest.raises(BuilderInvalid) as elsewhere:
            await _propose(db, office, [*invoice_checker_ops(office), _narrow_to_odoo()])
        assert elsewhere.value.code == "connection_not_allowed" and elsewhere.value.step == 4

        conn.department_id = office.department_id
        await db.flush()
        await _propose(db, office, [*invoice_checker_ops(office), _narrow_to_odoo()])


async def test_a_connection_revoked_after_proposing_refuses_apply(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, [*invoice_checker_ops(office), _narrow_to_odoo()])
        conn = (
            await db.execute(select(m.McpConnection).where(m.McpConnection.tenant_id == tenant))
        ).scalar_one()
        conn.connected = False
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "connection_not_allowed" and caught.value.step == 4
        assert proposal.status == "draft"
        assert proposal.result == {"refusal": "connection_not_allowed", "failedStep": 4}


async def test_a_taken_name_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        db.add(
            m.Agent(tenant_id=tenant, department_id=office.department_id, name="rechnungsprüfer")
        )
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _propose(db, office, invoice_checker_ops(office))
        assert caught.value.code == "name_taken"


async def test_apply_creates_the_whole_agent_once(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, invoice_checker_ops(office))
        agent_id = await _apply(db, office, proposal.id)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.model_config_id == office.model_id
        assert agent.status == "stopped"
        assignments = (
            (
                await db.execute(
                    select(m.SkillAssignment).where(m.SkillAssignment.agent_id == agent_id)
                )
            )
            .scalars()
            .all()
        )
        assert [a.skill_version_id for a in assignments] == [office.skill_version_id]
        triggers = (
            (await db.execute(select(m.Trigger).where(m.Trigger.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert len(triggers) == 1 and triggers[0].cron_expression == "0 7 * * 1-5"
        assert proposal.status == "applied" and proposal.result["agentId"] == str(agent_id)
        await flush_audit(db)
        audit = (
            await db.execute(
                select(m.AuditEvent).where(m.AuditEvent.action == "agent_builder.applied")
            )
        ).scalar_one()
        assert audit.resource["requested_by"] == "admin" and audit.resource["applied_by"] == "admin"
        # Ruling A13: the acting member, and both members by id.
        assert audit.actor_id == office.admin.member_id
        assert audit.resource["requested_by_member_id"] == str(office.admin.member_id)
        assert audit.resource["applied_by_member_id"] == str(office.admin.member_id)
        hires = (
            (
                await db.execute(
                    select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "hire_agent")
                )
            )
            .scalars()
            .all()
        )
        assert hires == [], "no hire gate in this tenant, so no hire request"

        with pytest.raises(BuilderInvalid) as again:
            await _apply(db, office, proposal.id)
        assert again.value.code == "already_applied"


async def test_apply_under_the_hire_gate_raises_the_hire_request(
    app_session: AppSessionFactory,
) -> None:
    """Ruling A11: the agent is held in pending_approval AND gets the hire
    request the agent-create route would raise, in the same transaction."""
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
        proposal = await _propose(db, office, invoice_checker_ops(office))
        agent_id = await _apply(db, office, proposal.id)
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.status == "pending_approval"
        hire = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "hire_agent")
            )
        ).scalar_one()
        assert hire.agent_id == agent_id
        assert hire.payload["model_config_id"] == str(office.model_id)


async def test_a_failing_step_rolls_everything_back(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        ops = [
            invoice_checker_ops(office)[0],
            {
                "type": "trigger.create",
                "agentRef": "$new",
                "kind": "event",
                "taskText": "x",
                "eventSource": "nowhere",
                "eventType": "never",
            },
        ]
        proposal = await _propose(db, office, ops)
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "step_failed" and caught.value.step == 1
        assert proposal.status == "draft"
        assert proposal.result == {
            "refusal": "step_failed",
            "failedStep": 1,
            "failedReason": "trigger_invalid",
        }
        created = (
            (await db.execute(select(m.Agent).where(m.Agent.name == "Rechnungsprüfer")))
            .scalars()
            .all()
        )
        assert created == [], "nothing is half-created"


async def _stored(db: Any, proposal: CopilotProposal) -> list[CopilotOperation]:
    return list(
        (
            await db.execute(
                select(CopilotOperation)
                .where(CopilotOperation.proposal_id == proposal.id)
                .order_by(CopilotOperation.ordinal)
            )
        ).scalars()
    )


async def test_a_tampered_row_with_a_second_create_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """The stored rows are parsed again before anything applies: a second
    `agent.create` slipped into storage never creates a second agent."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, invoice_checker_ops(office))
        rows = await _stored(db, proposal)
        db.add(
            CopilotOperation(
                tenant_id=tenant,
                proposal_id=proposal.id,
                ordinal=len(rows),
                operation_type=rows[0].operation_type,
                configuration={**rows[0].configuration, "name": "Zweiter"},
                target_revision=None,
            )
        )
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "two_refs" and caught.value.step == len(rows)
        assert proposal.status == "draft"
        agents = (
            (await db.execute(select(m.Agent).where(m.Agent.department_id == office.department_id)))
            .scalars()
            .all()
        )
        assert agents == []


async def test_a_reordered_stored_list_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, invoice_checker_ops(office))
        create, switch = (await _stored(db, proposal))[:2]
        create.configuration, switch.configuration = switch.configuration, create.configuration
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "ref_before_create" and caught.value.step == 0
        assert proposal.result == {"refusal": "ref_before_create", "failedStep": 0}


async def test_an_expired_proposal_cannot_be_applied(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(
            db, office, invoice_checker_ops(office), now=NOW - dt.timedelta(days=8)
        )
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "expired" and proposal.status == "expired"


async def test_the_generic_proposal_api_leaves_builder_proposals_alone(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, invoice_checker_ops(office))
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, _admin(office))
        assert proposal.status == "draft", "not marked expired by the stale check"
        with pytest.raises(ProposalNotRejectable):
            await reject_generic(db, proposal.id, _admin(office))
        assert proposal.status == "draft"


async def _gate_allows(
    db: Any, member: m.OrgMember, department_id: uuid.UUID, token_role: str
) -> bool:
    """`authorize_agent_write` answered for this member, as a yes/no."""
    principal = Principal(subject=member.subject, tenant_id=member.tenant_id, role=token_role)
    request = Request({"type": "http", "headers": [], "state": {}})
    # The gate reads its authority from the per-request memo; seed it with the
    # same off-request resolution `may_create_agent`'s callers use.
    request.state.oc8_authority = await authority_for_member(db, member, token_role=token_role)
    scope = await scope_for_member(db, member, token_role=token_role)
    actor = HumanActor(principal=principal, member=member, scope=scope)
    try:
        await authorize_agent_write(request, db, actor, department_id, not_found=HTTPException(404))
    except HTTPException:
        return False
    return True


@pytest.mark.parametrize(
    ("who", "token_role", "where", "expected"),
    [
        ("admin", "org_admin", "books", True),
        ("admin", "org_admin", "other", True),
        ("lead", "member", "books", True),
        ("lead", "member", "other", False),
        ("employee", "member", "books", False),
        ("employee", "member", "other", False),
    ],
)
async def test_may_create_agent_agrees_with_the_agent_write_gate(
    app_session: AppSessionFactory, who: str, token_role: str, where: str, expected: bool
) -> None:
    """Ruling A18: two statements of one rule, pinned to the same answers for
    tenant-wide, toggle and no-toggle callers."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        seat = getattr(office, who)
        department_id = office.department_id if where == "books" else office.other_department_id
        member = await db.get(m.OrgMember, seat.member_id)
        assert member is not None
        authority = await authority_for_member(db, member, token_role=token_role)
        scope = await scope_for_member(db, member, token_role=token_role)
        asked = may_create_agent(authority, scope, department_id)
        assert asked == await _gate_allows(db, member, department_id, token_role) == expected


async def test_a_skill_archived_after_proposing_refuses_apply(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, invoice_checker_ops(office))
        skill = (await db.execute(select(m.Skill).where(m.Skill.tenant_id == tenant))).scalar_one()
        skill.deleted_at = NOW
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "skill_not_allowed" and caught.value.step == 2


async def test_a_name_taken_after_proposing_refuses_apply_under_the_name_lock(
    app_session: AppSessionFactory,
) -> None:
    """The name check at apply runs under a transaction-scoped advisory lock on
    (tenant, lower(name)), and compares case-insensitively in SQL."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, invoice_checker_ops(office, name="Invoice Checker"))
        db.add(
            m.Agent(tenant_id=tenant, department_id=office.department_id, name="INVOICE checker")
        )
        await db.flush()
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "name_taken" and caught.value.step == 0
        # Another transaction cannot take the same (tenant, lower(name)) lock
        # until this one ends.
        async with app_session(tenant) as other:
            free = await other.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{tenant}:invoice checker"},
            )
        assert free is False


async def test_a_database_refusal_in_a_step_is_a_failed_step(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = apply_operation

    async def refusing(db: Any, *, tenant_id: uuid.UUID, data: dict[str, Any], **kw: Any) -> Any:
        if data["type"] == "agent.skill.assign":
            await db.execute(text("SELECT 1 / 0"))
        return await real(db, tenant_id=tenant_id, data=data, **kw)

    monkeypatch.setattr("oc8.copilot.builder.apply_operation", refusing)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await _propose(db, office, invoice_checker_ops(office))
        with pytest.raises(BuilderInvalid) as caught:
            await _apply(db, office, proposal.id)
        assert caught.value.code == "step_failed" and caught.value.step == 2
        assert proposal.result == {"refusal": "step_failed", "failedStep": 2}
        created = (
            (await db.execute(select(m.Agent).where(m.Agent.name == "Rechnungsprüfer")))
            .scalars()
            .all()
        )
        assert created == [], "the session survives and nothing is half-created"


async def test_a_concurrent_proposal_in_the_same_chat_loses_cleanly(
    app_session: AppSessionFactory,
) -> None:
    """Two proposes in one chat at once: the second one's FOR UPDATE cannot see
    the first's uncommitted draft, so the open-builder unique index decides,
    and the loser gets a code rather than an IntegrityError."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)

    async def second() -> None:
        async with app_session(tenant) as other:
            await _propose(other, office, invoice_checker_ops(office, name="Zweite"))

    async with app_session(tenant) as first:
        await _propose(first, office, invoice_checker_ops(office))
        racing = asyncio.create_task(second())
        await asyncio.sleep(0.5)
        assert not racing.done(), "the second insert waits on the first's index entry"
    with pytest.raises(BuilderInvalid) as caught:
        await racing
    assert caught.value.code == "superseded_concurrently"
    async with app_session(tenant) as db:
        open_rows = (
            (
                await db.execute(
                    select(CopilotProposal).where(
                        CopilotProposal.kind == BUILDER_KIND, CopilotProposal.status == "draft"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(open_rows) == 1
