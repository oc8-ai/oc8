"""Executors for the agent builder's tools (Copilot Agent Builder §3.1).

Same seam as the dot tools (oc8.copilot.tools): Copilot only, and every call
resolves the member behind the run's chat and acts as them. On top of that the
web door only -- the card is a web surface (Ruling A7) -- and the session's own
member only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import (
    BUILDER_OPTIONS,
    BUILDER_TOOL_NAMES,
    ENTER_AGENT_MODE,
    PROPOSE_BOARD,
    ControlOutcome,
    _acting_token_role,
    _resolve_agent_actor,
)
from oc8.agent.harness.prompts import resolve_timezone
from oc8.agents.repo import visible_agents
from oc8.authz.authority import Authority, authority_for_member, tenant_wide_read
from oc8.authz.permissions import AGENT as AGENT_RESOURCE
from oc8.authz.permissions import DEPARTMENT, VIEW, perm
from oc8.authz.scope import AgentActor
from oc8.boards.repo import visible_boards
from oc8.chat.model_choice import chat_enabled_model_ids
from oc8.chat.modes import (
    AGENT,
    ENTERED_MODE,
    mode_directive,
    mode_from_context,
    mode_refusal,
)
from oc8.connections.usable import department_systems, system_tool_scopes
from oc8.copilot.builder import (
    create_builder_proposal,
    may_create_agent,
    may_create_department,
)
from oc8.copilot.builder_ops import (
    FLAT_FIELDS,
    BuilderInvalid,
    flat_department_id,
    flat_new_department,
    proposal_operations,
)
from oc8.copilot.capabilities import new_department_frame
from oc8.copilot.door import door_of
from oc8.copilot.tools import uuid_or_none
from oc8.departments.repo import visible_departments
from oc8.edition.tools import tenant_has_capability
from oc8.licensing.capabilities import ENTERPRISE_CANVAS
from oc8.modelrouter import ToolCall
from oc8.runtime.fencing import fence, one_line
from oc8.runtime.run_context import merge_context

#: What propose_board takes; anything else is refused, never dropped.
_BOARD_FIELDS: frozenset[str] = frozenset(
    {"agentId", "boardId", "title", "description", "spec", "restoreVersion"}
)

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
    "skill_needs_system": (
        "a chosen skill needs a system the agent would not have; add that system "
        "to systems (builder_options lists the usable ones) or drop the skill"
    ),
    "unknown_field": "use only these fields: " + ", ".join(sorted(FLAT_FIELDS)),
    "mixed_shapes": "pass either the agent's fields or operations, not both",
    "event_config_without_event": (
        "eventConfig belongs to an event trigger: pass eventSource and eventType "
        "with it, or leave it out"
    ),
    "missing_agent": "give at least departmentId, name and mission",
    "department_create_not_allowed": (
        "this person may not create departments; pick the best-fitting existing "
        "department from builder_options and tell them why it is that one"
    ),
    "department_exists": "a department with this name exists; use its departmentId instead",
    "two_departments": "pass either departmentId or newDepartment, not both",
    "unknown_department_ref": (
        "an agent.create after department.create needs departmentRef, the ref of that "
        "department.create, and no departmentId"
    ),
    "awaiting_approval": (
        "the previous proposal is waiting for approval; tell the person to wait "
        "for the decision before changing it"
    ),
    "board_spec_invalid": (
        "the dashboard spec does not match the catalogue builder_options gives; "
        "check the datasets, fields, blocks and the keys they name"
    ),
    "board_builder_only": "dashboards are proposed with propose_agent or propose_board",
    "board_unknown": "pick a dashboard from builder_options (boardId)",
    "agent_unknown": "pick an agent from builder_options that may get a dashboard (agentId)",
    "agent_other_department": "a dashboard can only be handed to an agent of its own department",
    "board_version_unknown": "that version does not exist; leave restoreVersion out",
    "board_changed": (
        "the dashboard changed since this proposal; propose again from the current version"
    ),
    "artifact_not_available": "artifact blocks are not available here; use the other block types",
    "dataset_removed": "a dashboard change may only add datasets; keep every existing one",
    "field_removed": "a dashboard change may only add fields; keep every existing one",
    "grain_changed": "keep every dataset's grain; add a new dataset instead",
    "field_type_changed": "keep every field's type; add a new field instead",
    "currency_changed": "keep every field's currency; add a new field instead",
    "board_unknown_field": "use only these fields: " + ", ".join(sorted(_BOARD_FIELDS)),
    "missing_board_target": "pass agentId to create a dashboard or boardId to change one",
}

#: Losing the open-proposal race to a concurrent call is not the model's fault.
_RETRY = (
    "ERROR: this conversation's proposal was being changed at the same moment "
    "(saved, created or sent for approval), so nothing was saved. Call "
    "propose_agent or propose_board again with the same arguments; if that reports "
    "awaiting_approval, tell the person to wait for the decision."
)


def _refusal(exc: BuilderInvalid) -> str:
    if exc.code == "superseded_concurrently":
        return _RETRY
    where = f" at step {exc.step}" if exc.step is not None else ""
    hint = _REFUSALS.get(exc.code, "check the operations")
    return f"ERROR: proposal refused ({exc.code}{where}): {hint}"


def board_operations(arguments: object) -> list[dict[str, Any]]:
    """The one board operation a propose_board call stands for: a change when
    it names `boardId` (an `agentId` alongside hands the board to that agent),
    else a new board for `agentId`. Unvalidated -- `parse_builder_operations`
    judges it, exactly as for a board step of propose_agent."""
    if not isinstance(arguments, Mapping):
        raise BuilderInvalid("invalid")
    if set(arguments) - _BOARD_FIELDS:
        raise BuilderInvalid("board_unknown_field")
    # Small models send null or "" for a field they mean to leave out.
    given = {k: v for k, v in arguments.items() if v not in (None, "", [], {})}
    if "boardId" in given:
        return [{"type": "board.update", **given}]
    if "agentId" not in given or "restoreVersion" in given:
        raise BuilderInvalid("missing_board_target")
    return [{"type": "board.create", **given}]


#: The dashboard catalogue the model writes a spec from (spec §6.1); the
#: artifact block only for a tenant entitled to it (§6.7).
_CATALOGUE = (
    "Dashboard spec: {datasets: [{key, label, grain, fields: [{key, label, type, "
    "currency?}]}], blocks: [{type, title?, width?, ...}]}. Keys are lower_snake_case. "
    "Grains: day | week | month | moment. Field types: number | money (needs a "
    "three-letter currency) | percent | text | category | date. Blocks: kpi "
    "(dataset, field) | line, bar, area (dataset, fields, split_by?) | table "
    "(dataset, fields) | filter_bar (at most one, width 12) | note (dataset)"
)
_ARTIFACT_BLOCK = " | artifact (artifact_ref, datasets)"
_ADDITIVE = (
    "Width is 3, 4, 6, 8 or 12. A change (board.update, propose_board with "
    "boardId) may only add datasets and fields: never remove one or change its "
    "grain, type or currency."
)
#: How the figures add up and what a good board looks like: the model otherwise
#: stamps a batch total on every row and lays out a bare table.
_DESIGN = (
    "A kpi sums its field over the period's rows (percent: averages), so never "
    "store a precomputed total or count on each row: to count rows use a number "
    "field set to 1 per row; a headline figure that is not a sum of rows gets its "
    "own dataset with one row per period. Design: KPIs first (width 3 or 4), at "
    "least one chart (line or area over time for a trend, bar with split_by a "
    "category for a breakdown), table last (width 12) with the most useful "
    "fields, a filter_bar when there is a category to filter by, and a note "
    "block if the agent writes notes."
)


#: How to write the mission. An agent's prompt is its name, its mission and a
#: generic block (harness/prompts.render_system_prompt), so the mission alone
#: carries the job; without this the model writes two sentences.
_MISSION_GUIDE = (
    "Mission guide: the mission is the agent's full working instructions and the "
    "only part of its prompt that describes this job, so a short summary leaves "
    "it guessing. Write it in the person's language, addressed to the agent "
    '("you"), 150-400 words, with these sections in this order, each under a '
    "short heading: "
    "1. Goal -- what the agent is for and who gets the result. "
    "2. Procedure -- the numbered steps of one run, in order. "
    "3. Data -- which system, which records and fields, which filters (period, "
    "status). "
    "4. Rules -- what counts as noteworthy, thresholds, priorities. "
    "5. Result -- format and length of the answer, what to link. "
    "6. Special cases -- nothing found, data missing, a system unreachable, an "
    "ambiguous match. "
    "7. Limits -- what it must never do and what needs approval. "
    "Take thresholds and rules from the person; where they gave none, ask, or "
    "write a sensible default into the mission and mark it with the word "
    "default (in their language), so they see it on the card. Name a tool only "
    "when the tools list has it for that system, and never invent a field "
    "name: describe the data in business terms and let the agent look the "
    "fields up. Never put a password, a key or a record id into the mission. "
    "The agent never estimates or guesses a missing figure: Special cases says "
    "it reports what is missing. With a dashboard, Procedure names each "
    "dataset and field of the spec and the figure that goes into it, one row "
    "per period. Leave out what every agent is told anyway: read before "
    "changing, link records, report at the end."
)
#: Tool names listed per system and kind; the rest is counted, not dropped silently.
_TOOLS_SHOWN = 40


def _tool_names(names: list[str]) -> str:
    shown = ", ".join(one_line(n, 60) for n in sorted(names)[:_TOOLS_SHOWN]) or "-"
    more = len(names) - _TOOLS_SHOWN
    return f"{shown} (+{more} more)" if more > 0 else shown


async def _tool_lines(db: AsyncSession, tenant_id: uuid.UUID, keys: set[str]) -> list[str]:
    """What each usable system can do, so a mission names real steps. From
    the plugin manifests; a system without one gets no line."""
    lines = []
    for key in sorted(keys):
        scopes = await system_tool_scopes(db, tenant_id=tenant_id, key=key)
        reads = scopes.get("read", [])
        changes = [name for scope, names in scopes.items() if scope != "read" for name in names]
        if reads or changes:
            lines.append(f"- {one_line(key, 60)} | {_tool_names(reads)} | {_tool_names(changes)}")
    if not lines:
        return []
    return ["Tools per system (system | reads | changes):", *lines]


async def _board_lines(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: AgentActor,
    authority: Authority,
) -> list[str]:
    """The agents the member may give a dashboard and the dashboards they may
    change -- exactly where they may edit the producing agent (§6.6) -- and
    the spec catalogue. Agents and boards by their own reach, `agent:view`
    tenant-wide (as `api/v1/boards.py`), not the departments'."""
    tenant_wide = _reads_every_agent(authority)
    agents, _total = await visible_agents(db, scope=actor.scope, tenant_wide=tenant_wide)
    names: dict[uuid.UUID | None, str] = {a.id: a.name for a in agents}
    lines = ["Agents you may give a dashboard (agentId | name):"]
    lines.extend(
        f"- {a.id} | {one_line(a.name, 80)}"
        for a in sorted(agents, key=lambda a: a.name.lower())
        if may_create_agent(authority, actor.scope, a.department_id)
    )
    boards = await visible_boards(
        db, scope=actor.scope, tenant_wide=tenant_wide, tenant_id=tenant_id
    )
    lines.append("Dashboards you may change (boardId | title | agent | version):")
    lines.extend(
        f"- {b.id} | {one_line(b.title, 80)} | {one_line(names.get(b.agent_id, '-'), 80)} "
        f"| {b.spec_version}"
        for b in boards
        if may_create_agent(authority, actor.scope, b.department_id)
    )
    artifact = await tenant_has_capability(tenant_id, ENTERPRISE_CANVAS)
    lines.append(_CATALOGUE + (_ARTIFACT_BLOCK if artifact else "") + ". " + _ADDITIVE)
    lines.append(_DESIGN)
    return lines


def _reads_every_agent(authority: Authority) -> bool:
    """`tenant_wide` for the scoped agent and board repositories (§6.8.2)."""
    return tenant_wide_read(authority, perm(AGENT_RESOURCE, VIEW))


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
    if may_create_department(authority):
        how = (
            "create directly"
            if may_create_agent(authority, actor.scope, None)
            else "needs approval"
        )
        new_department = (
            "New department: allowed (newDepartment instead of departmentId, "
            f"{how}) -- only when the agent clearly belongs to none of these"
        )
    else:
        new_department = (
            "New department: not allowed for this person -- pick the best-fitting "
            "department below and say why"
        )
    lines = [
        new_department,
        "Departments (id | name | how | connected systems | not usable yet, and why):",
    ]
    own = actor.scope.viewable
    usable: set[str] = set()
    for dept in sorted(departments, key=lambda d: d.name.lower()):
        if not actor.scope.may_view(dept.id):
            # Only where a proposal may land (create_builder_proposal's check).
            continue
        how = (
            "create directly"
            if may_create_agent(authority, actor.scope, dept.id)
            else "needs approval"
        )
        systems = await department_systems(db, tenant_id=tenant_id, department=dept)
        usable.update(s.key for s in systems if s.usable)
        mine = " (their department)" if dept.id in own else ""
        listed = ", ".join(one_line(s.key, 60) for s in systems if s.usable) or "none"
        missing = (
            "; ".join(f"{one_line(s.key, 60)}: {s.missing}" for s in systems if s.missing) or "-"
        )
        lines.append(
            f"- {dept.id} | {one_line(dept.name, 80)}{mine} | {how} | {listed} | {missing}"
        )
    lines.extend(await _tool_lines(db, tenant_id, usable))
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
    lines.append(await _schedule_line(db, tenant_id))
    lines.extend(await _board_lines(db, tenant_id=tenant_id, actor=actor, authority=authority))
    lines.append(_MISSION_GUIDE)
    return fence("\n".join(lines), tag="builder_options")


async def _schedule_line(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Agent schedules are read in their trigger's timezone, by default the
    organisation's (triggers.service.create_trigger), and the card names it --
    so the model writes the person's own clock time and never converts."""
    org = await db.get(m.Organization, tenant_id)
    label, zone = resolve_timezone(org.timezone if org is not None else "UTC")
    offset = dt.datetime.now(zone).strftime("%z")
    return (
        "Schedules (schedule, cronExpression) run in their timezone, by default "
        f"this organisation's: {label} (now UTC{offset[:3]}:{offset[3:]}). Write the "
        "cron in the person's local time, e.g. 0 7 * * 1-5 for weekdays at 7:00; "
        "set timezone (an IANA name) only when the person is in another one."
    )


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
        # This run's next leg (after an ask_user park) reads the mode back
        # through mode_from_context, and its task carries the directive. The
        # engine switches the rest of THIS leg itself (Ruling A6). The chat
        # itself is switched at the leg's end (`chat.service.apply_entered_mode`),
        # not here: locking its row now would hold it, ahead of the tenant's
        # audit lock, while the model is called for the rest of the leg.
        await merge_context(
            db,
            run,
            {
                "chat_mode": AGENT.key,
                ENTERED_MODE: AGENT.key,
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

    # propose_agent, propose_board
    if context.get("chat_mode") != AGENT.key:
        return ControlOutcome(
            output=f"ERROR: {tc.name} works in Agent mode only; call enter_agent_mode first"
        )
    authority = await authority_for_member(db, actor.member, token_role=token_role)
    board = tc.name == PROPOSE_BOARD.name
    try:
        if board:
            operations: object = board_operations(tc.arguments)
        else:
            operations = proposal_operations(
                tc.arguments,
                # A new department gets the default frame (`department.create`).
                frame=new_department_frame()
                if flat_new_department(tc.arguments)
                else await _department_frame(db, tenant_id, flat_department_id(tc.arguments)),
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
                may_create_departments=may_create_department(authority),
                boards_tenant_wide=_reads_every_agent(authority),
            )
    except BuilderInvalid as exc:
        return ControlOutcome(output=_refusal(exc))
    # record_assistant_reply turns this into the answer's agent_builder part.
    await merge_context(db, run, {"builder_proposal_id": str(proposal.id)})
    return ControlOutcome(
        output=(
            f"Proposal {proposal.id} is shown to the person as a card. Nothing is "
            "created until they confirm it there. Say in one or two sentences what "
            f"the {'dashboard will show' if board else 'agent will do'}; do not "
            "repeat the card."
        )
    )
