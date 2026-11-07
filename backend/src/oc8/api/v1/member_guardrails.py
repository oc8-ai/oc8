"""A member's own guardrails per agent (D3a).

`me` routes: a member reads and sets their own tighten-only rules on exactly
the agents they can chat with (`chat.chat_agent_for` -- the same gate, so a
member without a seat can still set rules on the tenant's Copilot) and gets
404 for every other agent, existing or not. `members`: whoever manages the
agent reads everyone's rules; nobody writes another member's rules.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import DbSession, authorize_agent_write, require_agent_write, require_departmental
from oc8.api.v1.agents_write import _load_agent
from oc8.api.v1.chat import chat_agent_for
from oc8.authz.levels import LevelNotOffered, has_limits, level_of, member_entry_for_level
from oc8.authz.member_guardrail_store import (
    MemberGuardrailInvalid,
    MemberGuardrailWidens,
    agent_department,
    base_policies,
    member_row,
    set_member_tool,
)
from oc8.authz.pdp import (
    SubsetViolation,
    ToolPolicy,
    effective_tool_policies,
    tool_policy_source,
)
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.schemas.dto import (
    MemberGuardrailDTO,
    MyGuardrailsDTO,
    MyToolGuardrailDTO,
    SummaryLineDTO,
    ToolPolicyDTO,
)
from oc8.schemas.requests import MyGuardrailRequest

router = APIRouter()

#: The chat gate: an agent:view seat, or copilot:use for the tenant's Copilot.
Actor = Annotated[
    HumanActor,
    Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE)),
]

_NOT_FOUND = "agent not found"


def summary_lines(policies: dict[str, ToolPolicy]) -> list[SummaryLineDTO]:
    """What the agent may do for me, as codes the frontend turns into sentences."""
    groups: dict[str, list[str]] = {
        "may_modify": [],
        "may_read": [],
        "asks_you": [],
        "asks_department": [],
        "off": [],
    }
    thresholds: dict[str, list[int]] = {"asks_you": [], "asks_department": []}
    for key, p in sorted(policies.items()):
        lvl = level_of(p)
        if lvl == "off":
            groups["off"].append(key)
        elif lvl == "read_only":
            groups["may_read"].append(key)
        else:
            groups["may_modify"].append(key)
            if (
                p.member_approval_actions
                or p.member_approval_eur is not None
                or p.member_conditions
            ):
                groups["asks_you"].append(key)
                if p.member_approval_eur is not None:
                    thresholds["asks_you"].append(p.member_approval_eur)
            if p.approval_actions or p.approval_eur is not None or p.conditions:
                groups["asks_department"].append(key)
                if p.approval_eur is not None:
                    thresholds["asks_department"].append(p.approval_eur)
    lines: list[SummaryLineDTO] = []
    for code, tools in groups.items():
        if not tools:
            continue
        params: dict[str, Any] = {"tools": tools}
        if thresholds.get(code):
            params["threshold"] = min(thresholds[code])
        lines.append(SummaryLineDTO(code=code, params=params))
    return lines


async def _my_guardrails(
    db: DbSession, *, tenant_id: uuid.UUID, member: m.OrgMember, agent: m.Agent
) -> MyGuardrailsDTO:
    dept = await agent_department(db, agent)
    frame = dept.frame if dept else {}
    row = await member_row(db, tenant_id=tenant_id, member_id=member.id, agent_id=agent.id)
    mine_raw: dict[str, Any] = dict((row.policy if row else {}).get("tools", {}))
    base = effective_tool_policies(frame, agent.narrowing or {})
    effective = effective_tool_policies(frame, agent.narrowing or {}, {"tools": mine_raw})
    overridden = frozenset(agent.narrowing_overridden_keys or [])
    tools = [
        MyToolGuardrailDTO(
            tool_key=key,
            # The connection's name is its tool key (`McpConnection.name`).
            connection_name=key,
            base=ToolPolicyDTO(**base[key].to_json()),
            base_level=level_of(base[key]),
            mine=ToolPolicyDTO(**ToolPolicy.from_json(mine_raw[key]).to_json())
            if key in mine_raw
            else None,
            level=level_of(effective[key]),
            has_limits=has_limits(effective[key]),
            source=tool_policy_source(
                key,
                frame=frame,
                capa_defaults=dept.frame_capa_defaults if dept else None,
                narrowing_overridden_keys=overridden,
                member_keys=frozenset(mine_raw),
            ),
        )
        for key in sorted(base)
    ]
    return MyGuardrailsDTO(
        agent_id=str(agent.id),
        agent_name=agent.name,
        summary=summary_lines(effective),
        tools=tools,
    )


def _widens(violations: list[SubsetViolation]) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        {"error": "member_guardrail_widens", "violations": [v.__dict__ for v in violations]},
    )


async def _my_agent(
    request: Request, db: DbSession, actor: HumanActor, agent_id: uuid.UUID
) -> m.Agent:
    agent = await chat_agent_for(request, db, actor, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return agent


@router.get("/agents/{agent_id}/guardrails/me", response_model=MyGuardrailsDTO)
async def get_my_guardrails(
    agent_id: uuid.UUID, request: Request, db: DbSession, actor: Actor
) -> MyGuardrailsDTO:
    agent = await _my_agent(request, db, actor, agent_id)
    return await _my_guardrails(
        db, tenant_id=actor.principal.tenant_id, member=actor.member, agent=agent
    )


async def _write(
    db: DbSession, actor: HumanActor, agent: m.Agent, tool_key: str, entry: dict[str, Any] | None
) -> MyGuardrailsDTO:
    tenant_id = actor.principal.tenant_id
    try:
        await set_member_tool(
            db,
            tenant_id=tenant_id,
            member=actor.member,
            agent=agent,
            tool_key=tool_key,
            entry=entry,
        )
    except MemberGuardrailWidens as exc:
        raise _widens(exc.violations) from exc
    except MemberGuardrailInvalid as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "member_guardrail_invalid", "field": str(exc)},
        ) from exc
    return await _my_guardrails(db, tenant_id=tenant_id, member=actor.member, agent=agent)


@router.put("/agents/{agent_id}/guardrails/me/{tool_key}", response_model=MyGuardrailsDTO)
async def set_my_guardrail(
    agent_id: uuid.UUID,
    tool_key: str,
    body: MyGuardrailRequest,
    request: Request,
    db: DbSession,
    actor: Actor,
) -> MyGuardrailsDTO:
    agent = await _my_agent(request, db, actor, agent_id)
    base = await base_policies(db, agent)
    if tool_key not in base:
        # Every level -- "allowed" too, which otherwise only removes an entry
        # -- is refused alike for a tool the agent does not have.
        raise _widens([SubsetViolation(tool_key, "tool not granted by department or agent")])
    if body.level is None:
        entry = body.policy
    else:
        try:
            entry = member_entry_for_level(body.level, base[tool_key])
        except LevelNotOffered as exc:
            raise _widens([SubsetViolation(tool_key, str(exc))]) from exc
    return await _write(db, actor, agent, tool_key, entry)


@router.delete("/agents/{agent_id}/guardrails/me/{tool_key}", response_model=MyGuardrailsDTO)
async def delete_my_guardrail(
    agent_id: uuid.UUID, tool_key: str, request: Request, db: DbSession, actor: Actor
) -> MyGuardrailsDTO:
    agent = await _my_agent(request, db, actor, agent_id)
    return await _write(db, actor, agent, tool_key, None)


@router.get("/agents/{agent_id}/guardrails/members", response_model=list[MemberGuardrailDTO])
async def get_members_guardrails(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    actor: Annotated[HumanActor, Depends(require_agent_write())],
) -> list[MemberGuardrailDTO]:
    """Every member's own rules on this agent, read only: whoever manages the
    agent sees what its users asked for, and changes none of it."""
    agent = await _load_agent(db, agent_id)
    await authorize_agent_write(
        request,
        db,
        actor,
        agent.department_id,
        not_found=HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND),
    )
    rows = (
        await db.execute(
            select(m.MemberGuardrail, m.OrgMember)
            .join(m.OrgMember, m.OrgMember.id == m.MemberGuardrail.member_id)
            .where(
                m.MemberGuardrail.tenant_id == actor.principal.tenant_id,
                m.MemberGuardrail.agent_id == agent.id,
                m.OrgMember.deleted_at.is_(None),
            )
            .order_by(m.OrgMember.display_name, m.OrgMember.subject)
        )
    ).all()
    return [
        MemberGuardrailDTO(
            member_id=str(member.id),
            member_name=member.display_name or member.subject,
            tools={
                key: ToolPolicyDTO(**ToolPolicy.from_json(raw).to_json())
                for key, raw in sorted((row.policy or {}).get("tools", {}).items())
                if isinstance(raw, dict)
            },
        )
        for row, member in rows
    ]


@router.get("/copilot/guardrails", response_model=list[MyGuardrailsDTO])
async def get_my_guardrails_overview(
    request: Request, db: DbSession, actor: Actor
) -> list[MyGuardrailsDTO]:
    """Every agent I set a rule on and can still chat with, by agent name."""
    tenant_id = actor.principal.tenant_id
    agent_ids = (
        (
            await db.execute(
                select(m.MemberGuardrail.agent_id).where(
                    m.MemberGuardrail.tenant_id == tenant_id,
                    m.MemberGuardrail.member_id == actor.member.id,
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[MyGuardrailsDTO] = []
    for agent_id in agent_ids:
        agent = await chat_agent_for(request, db, actor, agent_id)
        if agent is not None:
            out.append(
                await _my_guardrails(db, tenant_id=tenant_id, member=actor.member, agent=agent)
            )
    return sorted(out, key=lambda dto: (dto.agent_name.lower(), dto.agent_id))
