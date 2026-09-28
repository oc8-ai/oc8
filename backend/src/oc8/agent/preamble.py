"""What every runtime must put in front of the model before the first step.

Extracted from the in-process engine so the isolated runtime seeds the SAME
context. It used to seed only the system prompt, which made an agent under
isolation strictly weaker than the same agent in-process: it could not name a
colleague to delegate to (no roster), could not invoke a skill it was never told
about (no catalog), and wrote memories nothing ever read back (no memory
context). Anything added here reaches both runtimes at once -- that is the whole
point of the module.

Core-neutral: names no vendor, product or software specifics.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import _acting_token_role, _resolve_agent_actor
from oc8.agent.provenance import RULE as PROVENANCE_RULE
from oc8.authz.authority import authority_for_member
from oc8.authz.permissions import (
    AGENT,
    APPROVAL,
    BUDGET,
    DEPARTMENT,
    STATISTICS,
    VIEW,
    perm,
)
from oc8.knowledge.retrieval import granted_kb_ids, retrieve_kb_context
from oc8.memory.router import retrieve_context
from oc8.modelrouter import NeutralMessage
from oc8.modelrouter.types import ImagePart, TextPart
from oc8.skills.runtime import LoadedSkill, catalog_block, load_assigned_skills


def system_prompt(agent: m.Agent, pinned: Mapping[str, Any] | None = None) -> str:
    """`pinned` is the run's resolved version (`resolve_version`); when given,
    role title and mission come from it rather than the live row, so a
    mid-run edit never rewrites the instructions a run is working to. `name`
    and `presentation` are identity/operational state, not versioned."""
    pres = agent.presentation or {}
    guardrails = pres.get("guardrails", [])
    role_title = pinned["role_title"] if pinned is not None else agent.role_title
    mission = pinned["mission"] if pinned is not None else agent.mission
    parts = [f"You are {agent.name}" + (f", {role_title}." if role_title else ".")]
    if mission:
        parts.append(mission)
    if guardrails:
        parts.append("Guardrails you must respect:\n" + "\n".join(f"- {g}" for g in guardrails))
    parts.append(
        "You have tools available. To use a tool you MUST invoke it through the "
        "function-calling interface — never write the tool call as text or JSON in "
        "your reply. Call one tool at a time and wait for its result. When the task "
        "is fully done, reply with a short plain-text summary and call no further tools."
    )
    parts.append(
        "Before you report completion, actively verify you covered the whole task, "
        "not just the first sub-part that happened to have work in it: re-read your "
        "own instructions and check whether anything else they describe still needs "
        "action right now -- including records or items that were already in "
        "progress before this run started, not only newly arrived ones. Only report "
        "'nothing to do' once you have actually checked, not because a first search "
        "came back empty. If you run out of steps before finishing, say so plainly "
        "in your summary instead of presenting a partial result as complete."
    )
    if agent.is_tenant_assistant:
        parts.append(
            "At the start of a new conversation, before waiting for the human to "
            "ask anything, proactively call list_pending_approvals and "
            "department_status (whichever of these you have been offered) and "
            "open with a short status summary -- what is waiting for a "
            "decision, and how the departments you can see are doing. Skip this "
            "if the conversation already has prior turns."
        )
    return "\n\n".join(parts)


@dataclass
class RunPreamble:
    """The seeded conversation plus what the caller needs downstream.

    ``skill_tool_names`` and ``contains_restricted`` are carried here rather than
    recomputed by each caller: the first decides which tool names bypass the
    frame check (getting it wrong is a security hole), the second decides whether
    a checkpoint may leave the tenant's locality. Deriving either twice is how
    the two runtimes drift apart.
    """

    messages: list[NeutralMessage]
    assigned_skills: list[LoadedSkill] = field(default_factory=list)
    skill_tool_names: frozenset[str] = frozenset()
    contains_restricted: bool = False
    #: Whether the agent (directly or via its department) has been granted at
    #: least one knowledge base. Independent of whether kb_ctx above actually
    #: found anything for THIS task's initial text -- search_knowledge exists
    #: precisely because a scheduled or blank-instruction run only learns its
    #: real topic mid-run, once it has read the record it was triggered for.
    has_knowledge: bool = False
    #: Whether the agent has at least one file attached to its own standing
    #: Instructions (FileAttachment(owner_type="agent_instructions")). Same
    #: shape as has_knowledge above: read_instruction_file exists precisely
    #: so that content is never auto-injected into every run's prompt, only
    #: fetched on demand.
    has_instruction_files: bool = False
    copilot_permissions: frozenset[str] = frozenset()


async def roster_block(db: AsyncSession, *, agent: m.Agent) -> str | None:
    """The agent's department colleagues, or None if it has none.

    A team lead can only name a real agent_id if it knows its colleagues, so
    without this delegate_task is offered but unusable -- every call denied as an
    invalid agent_id.

    Public because the container runtime has to build the same instructions from
    the outside. It was private, and the runtime plugin composed its standing
    file from system_prompt + catalog + delivery only -- so a lead in a container
    was offered delegate_task and could not name one colleague, while the same
    lead in-process could. Observed live: an accepted handoff told Sina to
    delegate, and her instructions never mentioned that Jan exists.

    The tenant Assistant is a special case: it sits alone in its own
    single-agent department (`get_or_create_assistant`), so a department-scoped
    roster is always empty for it -- yet `_member_may_reach_department` lets it
    delegate to ANY department the acting human can reach. Offered `delegate_task`
    with genuinely no names, it correctly has nothing to call: observed live, it
    fell back to asking the human "does your tenant have someone for this?" on
    every turn instead of ever delegating. It gets the full tenant roster
    (grouped by department) instead of its own department's -- naming the same
    agents its own delegation guard would actually let it reach.
    """
    if agent.is_tenant_assistant:
        rows = (
            await db.execute(
                select(m.Agent, m.Department.name)
                .join(m.Department, m.Department.id == m.Agent.department_id)
                .where(
                    m.Agent.tenant_id == agent.tenant_id,
                    m.Agent.id != agent.id,
                    m.Agent.status != "pending_approval",
                    m.Agent.deleted_at.is_(None),
                    m.Agent.is_tenant_assistant.is_(False),
                )
                .order_by(m.Department.name, m.Agent.name)
            )
        ).all()
        if not rows:
            return None
        lines = [
            f"- {a.id}: {a.name} ({dept}" + (f", {a.role_title})" if a.role_title else ")")
            for a, dept in rows
        ]
        return "Every agent in this tenant, by department -- delegate_task may reach any " \
            "of them if the person you are acting for can:\n" + "\n".join(lines)

    mates = (
        (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.department_id == agent.department_id,
                    m.Agent.id != agent.id,
                    m.Agent.status != "pending_approval",
                    m.Agent.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not mates:
        return None
    roster = "\n".join(
        f"- {a.id}: {a.name}" + (f" ({a.role_title})" if a.role_title else "") for a in mates
    )
    return f"Your department's agents:\n{roster}"


async def _gated_copilot_permissions(
    db: AsyncSession,
    *,
    agent: m.Agent,
    tenant_id: uuid.UUID,
    task: m.Task | None,
    run_id: uuid.UUID | None,
) -> frozenset[str]:
    """Which of the 5 status tools' permissions this run's Assistant may
    offer, mirroring `require_departmental`'s admission formula
    (`api/deps.py:282-359`) off-request, for the human behind this chat.

    Only ever non-empty for the tenant Assistant with a real chat-driven
    task behind it -- every other agent, and every non-chat origin
    (delegated/scheduled runs have no chat session), gets the empty set,
    which is exactly what `offered_tools` needs to withhold all 5 tools.
    """
    if not agent.is_tenant_assistant or task is None:
        return frozenset()
    agent_actor = await _resolve_agent_actor(
        db, tenant_id=tenant_id, task=task, run_id=run_id
    )
    if agent_actor is None:
        return frozenset()
    authority = await authority_for_member(
        db,
        agent_actor.member,
        token_role=await _acting_token_role(db, tenant_id=tenant_id, run_id=run_id),
    )
    granted = set()
    # Seat-grantable: a departmental seat alone is enough (SEAT_PERMISSIONS
    # includes all three of these for both SEAT_VIEWER and SEAT_APPROVER).
    for permission in (perm(APPROVAL, VIEW), perm(DEPARTMENT, VIEW), perm(AGENT, VIEW)):
        if permission in authority.tenant_wide or agent_actor.scope.holds_anywhere(
            permission
        ):
            granted.add(permission)
    # Tenant-wide only: not in SEAT_PERMISSIONS, so no seat can ever grant
    # these -- checked against authority.tenant_wide alone.
    for permission in (perm(BUDGET, VIEW), perm(STATISTICS, VIEW)):
        if permission in authority.tenant_wide:
            granted.add(permission)
    return frozenset(granted)


async def build_run_preamble(
    db: AsyncSession,
    *,
    agent: m.Agent,
    tenant_id: uuid.UUID,
    task_text: str,
    frame: dict[str, Any],
    model_locality: str,
    task_images: list[ImagePart] | None = None,
    supports_vision: bool = False,
    task: m.Task | None = None,
    run_id: uuid.UUID | None = None,
    pinned: Mapping[str, Any] | None = None,
) -> RunPreamble:
    """Seed a run's conversation: system context first, the task last.

    Message order is part of the contract -- the task must be the final turn, so
    the model reads its instructions against context already established.

    `pinned` is the run's resolved agent version (`resolve_version`). Every
    runtime passes it; None (a direct call with no run) reads the live row.
    """
    messages: list[NeutralMessage] = [
        NeutralMessage(role="system", content=system_prompt(agent, pinned))
    ]
    # Said once, before anything a stranger wrote can arrive. Every answer a
    # connection returns is fenced as <external>, and this is what makes that
    # fence mean something to the model. It is the cheap half of the defence --
    # the half that holds without the model's cooperation is the blast-radius
    # limit in agent/blast_radius.py.
    messages.append(NeutralMessage(role="system", content=PROVENANCE_RULE))

    memory_ctx = await retrieve_context(
        db,
        agent=agent,
        tenant_id=tenant_id,
        frame=frame,
        query_text=task_text,
        narrowing=(pinned["narrowing"] or {}) if pinned is not None else None,
    )
    if memory_ctx:
        messages.append(NeutralMessage(role="system", content=memory_ctx))

    kb_ctx, contains_restricted = await retrieve_kb_context(
        db,
        agent=agent,
        tenant_id=tenant_id,
        query_text=task_text,
        frame=frame,
        model_locality=model_locality,
    )
    if kb_ctx:
        messages.append(NeutralMessage(role="system", content=kb_ctx))
    has_knowledge = bool(await granted_kb_ids(db, agent=agent))

    instruction_file_count = (
        await db.scalar(
            select(func.count())
            .select_from(m.FileAttachment)
            .where(
                m.FileAttachment.tenant_id == tenant_id,
                m.FileAttachment.owner_type == "agent_instructions",
                m.FileAttachment.owner_id == agent.id,
            )
        )
        or 0
    )
    has_instruction_files = instruction_file_count > 0
    if has_instruction_files:
        # Not the file content itself -- that never enters the standing prompt
        # (see read_instruction_file's own docstring in control_tools.py) --
        # just enough of a nudge that the agent knows the tool exists and is
        # worth calling.
        messages.append(
            NeutralMessage(
                role="system",
                content=(
                    f"You have {instruction_file_count} attached reference "
                    f"file{'s' if instruction_file_count != 1 else ''}; use "
                    "read_instruction_file to read one by name."
                ),
            )
        )

    is_team_lead = bool(pinned["is_team_lead"]) if pinned is not None else agent.is_team_lead
    if is_team_lead:
        # Appended as its own system message (like memory/KB context) because
        # system_prompt is a pure sync function and this needs the DB.
        roster = await roster_block(db, agent=agent)
        if roster is not None:
            messages.append(NeutralMessage(role="system", content=roster))

    assigned_skills = await load_assigned_skills(db, agent=agent, tenant_id=tenant_id)
    catalog = catalog_block(assigned_skills)
    if catalog:
        messages.append(NeutralMessage(role="system", content=catalog))

    if task_images and supports_vision:
        messages.append(
            NeutralMessage(role="user", content=[TextPart(text=task_text), *task_images])
        )
    else:
        note = ""
        if task_images and not supports_vision:
            note = (
                "\n\n(An image was attached to this message, but this agent's "
                "model cannot process images.)"
            )
        messages.append(NeutralMessage(role="user", content=task_text + note))

    copilot_permissions = await _gated_copilot_permissions(
        db, agent=agent, tenant_id=tenant_id, task=task, run_id=run_id
    )
    return RunPreamble(
        messages=messages,
        assigned_skills=list(assigned_skills),
        # Only these names bypass the frame check in _authorize -- computed once
        # so a connection tool that merely happens to be named `skill_*` (an MCP
        # server can name anything) is never mistaken for an assigned skill.
        skill_tool_names=frozenset(s.tool_name for s in assigned_skills),
        contains_restricted=contains_restricted,
        has_knowledge=has_knowledge,
        has_instruction_files=has_instruction_files,
        copilot_permissions=copilot_permissions,
    )
