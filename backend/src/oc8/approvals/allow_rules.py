"""Always allow (for me): a person lets one write tool run without asking
them again.

A rule is one member's standing approval of one tool, for one agent on one
connection (by id: a connection re-created under the same name matches no old
rule). It only ever answers that member's OWN card, and only while all three
hold, re-checked every time it is used:

* the card is the member's own (`is_member_owned`, owner == rule's member);
* the held call is `write` tier, read off the card's stored `risk_tier` --
  destructive, irreversible and outward calls never are, whatever a rule says;
* the member may still decide the card now: the same checks the chat's
  decision door (`api/v1/chat_direct_access.py`) and `decide_approval` apply.

The answer goes through `decide_approval` like any person's, so the audit
line, the resume and the department's stage after the person
(`department_after_person`) are the ordinary ones: a rule decides the
person's card and never the department's.

Rule rows are read and written here only. Approval rows are loaded through
`approvals/repo.py`.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.approvals.repo import direct_access_owner, is_member_owned, pending_member_cards_of_run
from oc8.approvals.service import ApprovalError, decide_approval
from oc8.audit import append_event
from oc8.auth.principal import Principal
from oc8.authz.authority import authority_for_member
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor, scope_for_member
from oc8.db.base import uuid7

logger = logging.getLogger(__name__)

#: The only tier a rule may cover. The tier a card carries is the engine's
#: `classify_tier` result, which raises a call to destructive, irreversible
#: or outward from its annotations and its connection's declarations.
ALLOWED_TIER: Final = "write"

#: `HumanActor.via` for a decision a rule made: the audit line says so.
VIA: Final = "always_allow"

#: What the chat's decision door admits (`api/v1/chat_direct_access._ChatActor`).
_DOOR_PERMISSION: Final = perm(AGENT, VIEW)


class NotEligible(ApprovalError):
    """This card cannot become a rule: not the person's own, not write tier,
    or not approved by them. The routes answer 400."""


@dataclass(frozen=True)
class RuleView:
    rule: m.ToolAllowRule
    agent_name: str
    connection_name: str | None


def _uuid_or_none(raw: object) -> uuid.UUID | None:
    if raw is None or raw == "":
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def rule_eligible(approval: m.ApprovalRequest) -> bool:
    """May this card offer "always allow"? Only a person's own held write
    whose stored tier is `write`. A card without a tier (raised before the
    engine stored one, or by another raiser) is never eligible."""
    payload = approval.payload or {}
    return (
        approval.action_type == "tool_send"
        and is_member_owned(approval)
        and payload.get("risk_tier") == ALLOWED_TIER
        and isinstance(payload.get("tool"), str)
        and bool(payload.get("tool"))
    )


def _connection_of(approval: m.ApprovalRequest) -> uuid.UUID | None:
    return _uuid_or_none((approval.payload or {}).get("connection_id"))


async def _live_rule(
    db: AsyncSession,
    *,
    member_id: uuid.UUID,
    agent_id: uuid.UUID,
    connection_id: uuid.UUID | None,
    tool_key: str,
) -> m.ToolAllowRule | None:
    stmt = select(m.ToolAllowRule).where(
        m.ToolAllowRule.member_id == member_id,
        m.ToolAllowRule.agent_id == agent_id,
        m.ToolAllowRule.tool_key == tool_key,
        m.ToolAllowRule.revoked_at.is_(None),
        m.ToolAllowRule.connection_id.is_(None)
        if connection_id is None
        else m.ToolAllowRule.connection_id == connection_id,
    )
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalars().first()


async def create_rule_from_approval(
    db: AsyncSession, *, approval: m.ApprovalRequest, member_id: uuid.UUID
) -> m.ToolAllowRule:
    """The rule a person's approval of their own card asks for, in the
    caller's transaction. An existing live rule for the same terms is
    returned as it is."""
    if (
        not rule_eligible(approval)
        or direct_access_owner(approval) != member_id
        or approval.status != "approved"
    ):
        raise NotEligible("always allow covers only your own approved write")
    tool_key = str(approval.payload["tool"])
    connection_id = _connection_of(approval)
    # DO NOTHING, untargeted: the live-rule index is a partial expression
    # index, and a second click (or tab) must not fail the decision.
    await db.execute(
        pg_insert(m.ToolAllowRule)
        .values(
            id=uuid7(),
            tenant_id=approval.tenant_id,
            member_id=member_id,
            agent_id=approval.agent_id,
            connection_id=connection_id,
            tool_key=tool_key,
            created_from_approval_id=approval.id,
        )
        .on_conflict_do_nothing()
    )
    rule = await _live_rule(
        db,
        member_id=member_id,
        agent_id=approval.agent_id,
        connection_id=connection_id,
        tool_key=tool_key,
    )
    if rule is None:  # pragma: no cover - READ COMMITTED sees the winner
        raise RuntimeError("could not create or re-read the allow rule")
    await append_event(
        db,
        tenant_id=approval.tenant_id,
        actor_type="operator",
        actor_id=member_id,
        category="approval",
        action="approval.allow_rule_created",
        resource={
            "rule_id": str(rule.id),
            "approval_id": str(approval.id),
            "agent_id": str(rule.agent_id),
            "connection_id": str(connection_id) if connection_id else None,
            "tool_key": tool_key,
            "member_id": str(member_id),
        },
    )
    return rule


async def _deciding_member(
    db: AsyncSession, *, approval: m.ApprovalRequest, rule: m.ToolAllowRule, run: m.AgentRun
) -> HumanActor | None:
    """The rule's member as the actor of this decision, or None when they
    may not decide the card now. The chat door's admission, resolved
    off-request: a live member row, and `agent:view` anywhere or
    `copilot:use` tenant-wide -- with the token claim this run recorded, and
    only when it is this member's (`chat/service` writes it for the turn's
    own sender)."""
    if direct_access_owner(approval) != rule.member_id:
        return None
    member = await db.get(m.OrgMember, rule.member_id, populate_existing=True)
    if member is None or member.deleted_at is not None or member.tenant_id != run.tenant_id:
        return None
    ctx = run.context if isinstance(run.context, dict) else {}
    claim = ctx.get("operator_role")
    token_role = (
        claim
        if run.source == "chat"
        and isinstance(claim, str)
        and claim
        and ctx.get("originating_operator") == member.subject
        else None
    )
    authority = await authority_for_member(db, member, token_role=token_role)
    scope = await scope_for_member(db, member, token_role=token_role)
    if not (
        _DOOR_PERMISSION in authority.tenant_wide
        or scope.holds_anywhere(_DOOR_PERMISSION)
        or COPILOT_USE in authority.tenant_wide
    ):
        return None
    # No token behind this decision: the principal names the member and the
    # claim the run recorded. `decide_approval` reads no permission off it for
    # a held write (EFFECT_PERMISSIONS["tool_send"] is None); the audit takes
    # its subject.
    principal = Principal(
        subject=member.subject, tenant_id=member.tenant_id, role=token_role or "", kind="operator"
    )
    return HumanActor(principal=principal, member=member, scope=scope, via=VIA)


async def auto_decide_by_rules(db: AsyncSession, *, run: m.AgentRun) -> list[uuid.UUID]:
    """Approve the person's pending cards this run just parked on that a
    live rule of theirs covers. Returns the runs to publish once the caller
    commits (the resumed run).

    Each card in its own savepoint: one that cannot be decided stays pending
    and the others are still answered. A department's stage raised by the
    person's approval is not a member card and is never answered here."""
    if run.task_id is None:
        return []
    resumed: list[uuid.UUID] = []
    for approval in await pending_member_cards_of_run(db, task_id=run.task_id, run_id=run.id):
        if not rule_eligible(approval):
            continue
        owner = direct_access_owner(approval)
        if owner is None:
            continue
        rule = await _live_rule(
            db,
            member_id=owner,
            agent_id=approval.agent_id,
            connection_id=_connection_of(approval),
            tool_key=str(approval.payload["tool"]),
        )
        if rule is None:
            continue
        # A rolled-back savepoint expires both rows; log ids read before it.
        rule_id, approval_id = rule.id, approval.id
        try:
            async with db.begin_nested():
                actor = await _deciding_member(db, approval=approval, rule=rule, run=run)
                if actor is None:
                    continue
                result = await decide_approval(
                    db,
                    approval,
                    decision="approve",
                    tenant_id=approval.tenant_id,
                    actor=actor,
                    reason=f"always-allow rule {rule.id}",
                )
                await append_event(
                    db,
                    tenant_id=approval.tenant_id,
                    actor_type="operator",
                    actor_id=rule.member_id,
                    category="approval",
                    action="approval.allow_rule_used",
                    resource={
                        "rule_id": str(rule.id),
                        "approval_id": str(approval.id),
                        "agent_id": str(approval.agent_id),
                        "connection_id": str(rule.connection_id) if rule.connection_id else None,
                        "tool_key": rule.tool_key,
                        "member_id": str(rule.member_id),
                        "run_id": str(run.id),
                    },
                    principal=actor.principal,
                )
        except Exception:
            logger.warning(
                "always-allow rule %s could not decide approval %s; it stays pending",
                rule_id,
                approval_id,
                exc_info=True,
            )
            continue
        if result.resumed_run_id is not None and result.resumed_run_id not in resumed:
            resumed.append(result.resumed_run_id)
    return resumed


async def member_rules(db: AsyncSession, *, member_id: uuid.UUID) -> list[RuleView]:
    """The member's own live rules, newest first, with the agent's and the
    connection's names (None when the connection is gone)."""
    stmt = (
        select(m.ToolAllowRule, m.Agent.name, m.McpConnection.name)
        .outerjoin(m.Agent, m.Agent.id == m.ToolAllowRule.agent_id)
        .outerjoin(m.McpConnection, m.McpConnection.id == m.ToolAllowRule.connection_id)
        .where(m.ToolAllowRule.member_id == member_id, m.ToolAllowRule.revoked_at.is_(None))
        .order_by(m.ToolAllowRule.created_at.desc(), m.ToolAllowRule.id.desc())
    )
    return [
        RuleView(rule=rule, agent_name=agent_name or "", connection_name=connection_name)
        for rule, agent_name, connection_name in (await db.execute(stmt)).all()
    ]


async def revoke_rule(db: AsyncSession, *, rule_id: uuid.UUID, member_id: uuid.UUID) -> bool:
    """Revoke one of the member's own live rules. False for "no such rule"
    and for "somebody else's" alike: the caller answers both with one 404."""
    rule = (
        await db.execute(
            select(m.ToolAllowRule)
            .where(
                m.ToolAllowRule.id == rule_id,
                m.ToolAllowRule.member_id == member_id,
                m.ToolAllowRule.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if rule is None:
        return False
    rule.revoked_at = dt.datetime.now(dt.UTC)
    await db.flush()
    await append_event(
        db,
        tenant_id=rule.tenant_id,
        actor_type="operator",
        actor_id=member_id,
        category="approval",
        action="approval.allow_rule_revoked",
        resource={
            "rule_id": str(rule.id),
            "agent_id": str(rule.agent_id),
            "connection_id": str(rule.connection_id) if rule.connection_id else None,
            "tool_key": rule.tool_key,
            "member_id": str(member_id),
        },
    )
    return True
