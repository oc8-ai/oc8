"""Executors for the agent builder's tools (Copilot Agent Builder §3.1).

Same seam as the dot tools (oc8.copilot.tools): Copilot only, and every call
resolves the member behind the run's chat and acts as them. On top of that the
web door only -- the card is a web surface (Ruling A7) -- and the session's own
member only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import (
    BUILDER_OPTIONS,
    BUILDER_TOOL_NAMES,
    ENTER_AGENT_MODE,
    ControlOutcome,
    _acting_token_role,
    _resolve_agent_actor,
)
from oc8.authz.authority import Authority, authority_for_member, tenant_wide_read
from oc8.authz.permissions import DEPARTMENT, VIEW, perm
from oc8.authz.scope import AgentActor
from oc8.chat.model_choice import chat_enabled_model_ids
from oc8.chat.modes import AGENT, mode_directive, mode_from_context, mode_refusal
from oc8.copilot.builder import create_builder_proposal, may_create_agent, usable_systems
from oc8.copilot.builder_ops import (
    FLAT_FIELDS,
    BuilderInvalid,
    flat_department_id,
    proposal_operations,
)
from oc8.copilot.door import door_of
from oc8.copilot.tools import uuid_or_none
from oc8.departments.repo import visible_departments
from oc8.modelrouter import ToolCall
from oc8.runtime.fencing import fence, one_line
from oc8.runtime.run_context import merge_context

#: What the model is told for each refusal code -- value-free, actionable.
_REFUSALS: dict[str, str] = {
    "invalid": "an operation does not match its fields",
    "missing_create": "the first operation must be agent.create",
    "missing_ref": 'agent.create needs ref "$new"',
    "two_refs": "propose exactly one new agent",
    "unknown_ref": 'every later step needs agentRef "$new", the ref of the agent.create',
    "agent_id_not_allowed": "use agentRef, never an agentId",
    "type_not_allowed": "only agent settings may follow agent.create",
    "department_unknown": "pick a department from builder_options",
    "secret_value": "remove the credential value; secrets are set up elsewhere",
    "name_taken": "an agent with this name exists; pick another name",
    "model_not_allowed": "pick a model from builder_options",
    "skill_not_allowed": "pick a skill from builder_options",
    "connection_not_allowed": "use only systems builder_options lists for that department",
    "unknown_field": "use only these fields: " + ", ".join(sorted(FLAT_FIELDS)),
    "mixed_shapes": "pass either the agent's fields or operations, not both",
    "missing_agent": "give at least departmentId, name and mission",
    "awaiting_approval": (
        "the previous proposal is waiting for approval; tell the person to wait "
        "for the decision before changing it"
    ),
}

#: Losing the open-proposal race to a concurrent call is not the model's fault.
_RETRY = (
    "ERROR: this conversation's proposal was being changed at the same moment "
    "(saved, created or sent for approval), so nothing was saved. Call "
    "propose_agent again with the same operations; if that reports "
    "awaiting_approval, tell the person to wait for the decision."
)


def _refusal(exc: BuilderInvalid) -> str:
    if exc.code == "superseded_concurrently":
        return _RETRY
    where = f" at step {exc.step}" if exc.step is not None else ""
    hint = _REFUSALS.get(exc.code, "check the operations")
    return f"ERROR: proposal refused ({exc.code}{where}): {hint}"


def _reads_every_department(authority: Authority) -> bool:
    """`tenant_wide` for the scoped department repository, as the Copilot's
    department_status tool derives it."""
    return tenant_wide_read(authority, perm(DEPARTMENT, VIEW))


async def _department_frame(
    db: AsyncSession, tenant_id: uuid.UUID, department_id: str | None
) -> dict[str, Any] | None:
    """The frame a flat proposal's systems keep their rights from. Only a
    rights lookup: whether the member may use the department and its systems
    is `create_builder_proposal`'s check, exactly as for explicit operations."""
    dept_id = uuid_or_none(department_id)
    dept = await db.get(m.Department, dept_id) if dept_id else None
    if dept is None or dept.tenant_id != tenant_id or not isinstance(dept.frame, dict):
        return None
    return dept.frame


async def builder_options_text(
    db: AsyncSession, *, tenant_id: uuid.UUID, actor: AgentActor, token_role: str | None
) -> str:
    """What the member may choose, fenced: department, system, model and skill
    names are tenant data, never instructions (Ruling 38). Systems are the
    usable ones only (Ruling A12) -- exactly what a proposal may select."""
    authority = await authority_for_member(db, actor.member, token_role=token_role)
    departments, _total = await visible_departments(
        db, scope=actor.scope, tenant_wide=_reads_every_department(authority)
    )
    lines = ["Departments (id | name | how | connected systems):"]
    own = actor.scope.viewable
    for dept in sorted(departments, key=lambda d: d.name.lower()):
        if not actor.scope.may_view(dept.id):
            # Only where a proposal may land (create_builder_proposal's check).
            continue
        how = (
            "create directly"
            if may_create_agent(authority, actor.scope, dept.id)
            else "needs approval"
        )
        systems = sorted(await usable_systems(db, tenant_id=tenant_id, department=dept))
        mine = " (their department)" if dept.id in own else ""
        listed = ", ".join(one_line(s, 60) for s in systems) or "none"
        lines.append(f"- {dept.id} | {one_line(dept.name, 80)}{mine} | {how} | {listed}")
    model_ids = await chat_enabled_model_ids(db, tenant_id=tenant_id)
    models = (
        (await db.execute(select(m.ModelConfig).where(m.ModelConfig.id.in_(model_ids))))
        .scalars()
        .all()
        if model_ids
        else []
    )
    lines.append("Models (modelConfigId | label):")
    lines.extend(
        f"- {mc.id} | {one_line(mc.display_name or mc.model, 80)}"
        for mc in sorted(models, key=lambda c: (c.display_name or c.model).lower())
    )
    skills = (
        (
            await db.execute(
                select(m.Skill)
                .where(
                    m.Skill.tenant_id == tenant_id,
                    m.Skill.deleted_at.is_(None),
                    m.Skill.current_version_id.is_not(None),
                )
                .order_by(m.Skill.name)
            )
        )
        .scalars()
        .all()
    )
    lines.append("Skills (skillVersionId | name):")
    lines.extend(f"- {s.current_version_id} | {one_line(s.name, 80)}" for s in skills)
    return fence("\n".join(lines), tag="builder_options")


async def execute_builder_tool(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    task: m.Task,
    tc: ToolCall,
    run_id: uuid.UUID | None,
) -> ControlOutcome | None:
    """Run one builder tool, or return None for a name this module does not own."""
    if tc.name not in BUILDER_TOOL_NAMES:
        return None
    if not agent.is_tenant_assistant:
        return ControlOutcome(output="ERROR: only the Copilot sets up agents")
    actor = await _resolve_agent_actor(db, tenant_id=tenant_id, task=task, run_id=run_id)
    if actor is None:
        return ControlOutcome(
            output="ERROR: only the person this conversation belongs to can set up an agent here"
        )
    run = await db.get(m.AgentRun, run_id) if run_id else None
    context: dict[str, Any] = (run.context or {}) if run is not None else {}
    if run is None or door_of(context) != "web":
        return ControlOutcome(
            output=(
                "ERROR: agents are set up in the web chat; tell the person to open "
                "the Copilot there"
            )
        )
    session_id = uuid_or_none(context.get("chat_session_id"))
    session = await db.get(m.ChatSession, session_id) if session_id else None
    if session is None or session.tenant_id != tenant_id or session.member_id != actor.member.id:
        return ControlOutcome(output="ERROR: agents are set up in the person's own conversation")
    token_role = await _acting_token_role(db, tenant_id=tenant_id, run_id=run_id)
    refused = mode_refusal(mode_from_context(context), tc.name, tool_scopes=None)
    if refused is not None:
        # The PEP decided the same already; the dispatch does not rely on it
        # (Ruling A22: a mode may only narrow, so /plan cannot enter Agent mode).
        return ControlOutcome(output=f"ERROR: {refused}")

    if tc.name == ENTER_AGENT_MODE.name:
        session.mode = AGENT.key
        # This run's next leg (after an ask_user park) reads the mode back
        # through mode_from_context, and its task carries the directive. The
        # engine switches the rest of THIS leg itself (Ruling A6).
        await merge_context(
            db,
            run,
            {
                "chat_mode": AGENT.key,
                "task": str(context.get("task", "")) + "\n\n" + mode_directive(AGENT),
            },
        )
        await db.flush()
        options = await builder_options_text(
            db, tenant_id=tenant_id, actor=actor, token_role=token_role
        )
        return ControlOutcome(
            output=f"Agent mode is on for this conversation. {AGENT.instruction}\n\n{options}"
        )

    if tc.name == BUILDER_OPTIONS.name:
        return ControlOutcome(
            output=await builder_options_text(
                db, tenant_id=tenant_id, actor=actor, token_role=token_role
            )
        )

    # propose_agent
    if context.get("chat_mode") != AGENT.key:
        return ControlOutcome(
            output="ERROR: propose_agent works in Agent mode only; call enter_agent_mode first"
        )
    authority = await authority_for_member(db, actor.member, token_role=token_role)
    try:
        operations = proposal_operations(
            tc.arguments,
            frame=await _department_frame(db, tenant_id, flat_department_id(tc.arguments)),
        )
        # A savepoint: a refused proposal leaves nothing behind -- neither the
        # draft it would have superseded nor a lost race's half (Task 4).
        async with db.begin_nested():
            proposal = await create_builder_proposal(
                db,
                tenant_id=tenant_id,
                member=actor.member,
                scope=actor.scope,
                tenant_wide=_reads_every_department(authority),
                session_id=session.id,
                operations=operations,
                now=dt.datetime.now(tz=dt.UTC),
            )
    except BuilderInvalid as exc:
        return ControlOutcome(output=_refusal(exc))
    # record_assistant_reply turns this into the answer's agent_builder part.
    await merge_context(db, run, {"builder_proposal_id": str(proposal.id)})
    return ControlOutcome(
        output=(
            f"Proposal {proposal.id} is shown to the person as a card. Nothing is "
            "created until they confirm it there. Say in one or two sentences what "
            "the agent will do; do not repeat the card."
        )
    )
