"""Writing a member's guardrails (D3a). The only writer of member_guardrail.

Shared by the API (`api/v1/member_guardrails.py`) and the Copilot's card. Every
writer of a department frame or an agent narrowing calls `prune_redundant` in
the same transaction, so a member rule the agent's own guardrails now cover
disappears for good (design review D5: "rule became redundant").
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.auth.principal import Principal
from oc8.authz.pdp import (
    SubsetViolation,
    ToolPolicy,
    effective_tool_policies,
    member_term_violations,
)


class MemberGuardrailWidens(Exception):
    def __init__(self, violations: list[SubsetViolation]) -> None:
        super().__init__("member guardrail would widen department or agent")
        self.violations = violations


class MemberGuardrailInvalid(Exception):
    """The entry is not a tool policy this module can read back."""


async def agent_department(db: AsyncSession, agent: m.Agent) -> m.Department | None:
    """The department whose frame bounds `agent` -- read only for an agent the
    caller has already been shown."""
    return await db.get(m.Department, agent.department_id)


async def base_policies(db: AsyncSession, agent: m.Agent) -> dict[str, ToolPolicy]:
    """Department ∩ agent for `agent`: what a member term may only tighten."""
    dept = await agent_department(db, agent)
    return effective_tool_policies(dept.frame if dept else {}, agent.narrowing or {})


async def member_row(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, agent_id: uuid.UUID
) -> m.MemberGuardrail | None:
    return (
        await db.execute(
            select(m.MemberGuardrail).where(
                m.MemberGuardrail.tenant_id == tenant_id,
                m.MemberGuardrail.member_id == member_id,
                m.MemberGuardrail.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()


def _checked(entry: dict[str, Any]) -> None:
    """Fail on an entry that would only break later, at decision time."""
    eur = entry.get("approval_eur")
    if eur is not None and (isinstance(eur, bool) or not isinstance(eur, int) or eur < 0):
        raise MemberGuardrailInvalid("approval_eur")
    for name in ("approval_actions", "only", "conditions"):
        value = entry.get(name)
        if value is not None and not isinstance(value, list):
            raise MemberGuardrailInvalid(name)
    try:
        ToolPolicy.from_json(entry)
    except (KeyError, TypeError, ValueError) as exc:
        raise MemberGuardrailInvalid(str(exc)) from exc


async def set_member_tool(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member: m.OrgMember,
    agent: m.Agent,
    tool_key: str,
    entry: dict[str, Any] | None,
) -> None:
    """Set (or, with `entry=None`, remove) one tool of a member's guardrail.

    Tighten only: raises `MemberGuardrailWidens` for anything department ∩
    agent does not grant. An entry that adds nothing to what the agent already
    does is not stored, for the same reason `prune_redundant` drops one.
    """
    row = await member_row(db, tenant_id=tenant_id, member_id=member.id, agent_id=agent.id)
    tools = dict((row.policy if row else {}).get("tools", {}))
    if entry is None:
        tools.pop(tool_key, None)
    else:
        _checked(entry)
        base = await base_policies(db, agent)
        violations = member_term_violations(base, {"tools": {tool_key: entry}})
        if violations:
            raise MemberGuardrailWidens(violations)
        if _adds_nothing(base, tool_key, entry):
            tools.pop(tool_key, None)
        else:
            tools[tool_key] = entry
    if row is None and tools:
        db.add(
            m.MemberGuardrail(
                tenant_id=tenant_id,
                member_id=member.id,
                agent_id=agent.id,
                policy={"tools": tools},
            )
        )
    elif row is not None and tools:
        row.policy = {"tools": tools}
    elif row is not None:
        await db.delete(row)
    await db.flush()


async def prune_redundant(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    principal: Principal | None = None,
) -> list[tuple[uuid.UUID, str]]:
    """Drop member entries the agent's own guardrails now cover (design
    review D5: "rule became redundant"). Called by every writer of the
    frame or the narrowing, in the same transaction.

    One audit line per member who lost an entry -- the Copilot keeps no
    per-member activity feed to write it to.
    """
    base = await base_policies(db, agent)
    removed: list[tuple[uuid.UUID, str]] = []
    rows = (
        (
            await db.execute(
                select(m.MemberGuardrail).where(
                    m.MemberGuardrail.tenant_id == tenant_id,
                    m.MemberGuardrail.agent_id == agent.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        tools = dict((row.policy or {}).get("tools", {}))
        gone = [
            key
            for key, raw in tools.items()
            if key not in base or not isinstance(raw, dict) or _adds_nothing(base, key, raw)
        ]
        if not gone:
            continue
        for key in gone:
            tools.pop(key)
            removed.append((row.member_id, key))
        if tools:
            row.policy = {"tools": tools}
        else:
            await db.delete(row)
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="operator",
            actor_id=None,
            category="authz",
            action="member_guardrail.pruned",
            resource={
                "agent_id": str(agent.id),
                "member_id": str(row.member_id),
                "tools": sorted(gone),
                # Rendered by the frontend: "Your guardrail for {tool} on
                # {agent} is now set by the department."
                "message_code": "member_guardrail.now_set_by_department",
            },
            principal=principal,
        )
    await db.flush()
    return removed


def _adds_nothing(base: dict[str, ToolPolicy], key: str, raw: dict[str, Any]) -> bool:
    """True when the member entry is already implied by department ∩ agent."""
    b = base[key]
    p = effective_tool_policies({"tools": {key: b.to_json()}}, {}, {"tools": {key: raw}})[key]
    same_rights = (p.enabled, p.read, p.modify, p.only) == (b.enabled, b.read, b.modify, b.only)
    eur_covered = p.member_approval_eur is None or (
        b.approval_eur is not None and b.approval_eur <= p.member_approval_eur
    )
    return (
        same_rights
        and p.member_approval_actions <= b.approval_actions
        and eur_covered
        and all(c in b.conditions for c in p.member_conditions)
    )
