"""`guardrail_suggest`: propose one of the member's own guardrails as a card.

Writes nothing. The member's Confirm on the card calls their own
`PUT /agents/{id}/guardrails/me/{toolKey}`, so the same rules (reach, never
widen) are enforced there again; this only keeps the Copilot from offering a
card that Confirm would refuse, or for an agent the member cannot reach.
"""

from __future__ import annotations

import uuid
from typing import Any, get_args

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.authority import authority_for_member
from oc8.authz.levels import Level, LevelNotOffered, level_of, member_entry_for_level
from oc8.authz.member_guardrail_store import base_policies
from oc8.authz.pdp import member_term_violations
from oc8.authz.scope import DepartmentScope
from oc8.chat.reach import reachable_chat_agent

#: What may be suggested. "allowed" removes a rule; it is not one.
SUGGESTABLE: frozenset[str] = frozenset(get_args(Level)) - {"allowed"}

#: One text for "no such agent" and "not yours to chat with".
_UNREACHABLE = "ERROR: no such agent among the ones you can chat with"


def _id(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


async def suggest_guardrail(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member: m.OrgMember,
    scope: DepartmentScope,
    token_role: str | None,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """`(output, rendered_component)`; the component is None on an ERROR."""
    agent_id = _id(args.get("agent_id"))
    if agent_id is None:
        return _UNREACHABLE, None
    # The member's authority as every other door resolves it. No token exists
    # inside a run, so the claim the chat API recorded stands in; none means the
    # row terms alone decide, which is the fail-closed direction.
    authority = await authority_for_member(db, member, token_role=token_role)
    agent = await reachable_chat_agent(
        db, tenant_id=tenant_id, scope=scope, authority=authority, agent_id=agent_id
    )
    if agent is None:
        return _UNREACHABLE, None
    tool_key = args.get("tool_key")
    level = args.get("level")
    if not isinstance(tool_key, str) or not tool_key:
        return "ERROR: tool_key is required", None
    if level not in SUGGESTABLE:
        return "ERROR: level must be off, read_only or ask_first", None
    base = await base_policies(db, agent)
    if tool_key not in base:
        return f"ERROR: {agent.name} has no tool {tool_key!r}", None
    try:
        entry = member_entry_for_level(level, base[tool_key])
    except LevelNotOffered as exc:
        return f"ERROR: {exc}", None
    violations = member_term_violations(base, {"tools": {tool_key: entry}})
    if violations:
        return f"ERROR: {violations[0].reason}", None
    base_level = level_of(base[tool_key])
    return (
        f"Suggestion shown ({agent.name}, {tool_key}: {level}). It is not active until "
        "the person confirms the card.",
        {
            "component_key": "guardrail_card",
            "props": {
                "agentId": str(agent.id),
                "agentName": agent.name,
                "toolKey": tool_key,
                "level": level,
                "baseLevel": base_level,
            },
        },
    )
