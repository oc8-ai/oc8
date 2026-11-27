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

import datetime as dt
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import (
    _acting_token_role,
    _owner_claim,
    _resolve_agent_actor,
    copilot_person,
    person_token_role,
)
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.harness.prompts import (
    render_system_prompt,
    resolve_timezone,
    run_context_block,
)
from oc8.agent.provenance import RULE as PROVENANCE_RULE
from oc8.authz.authority import authority_for_member
from oc8.authz.pdp import effective_tool_policies, reachable_connections
from oc8.authz.permissions import (
    AGENT,
    APPROVAL,
    BUDGET,
    DEPARTMENT,
    STATISTICS,
    VIEW,
    perm,
)
from oc8.authz.scope import AgentActor
from oc8.chat.replay import (
    ChatReplay,
    last_answer,
    new_turn_text,
    replay_messages,
    replay_path,
    withheld,
)
from oc8.chat.turn_context import flattened_task_text_for_model
from oc8.connections.tool_connection import tool_connections
from oc8.connections.usable import needs_department
from oc8.copilot.notes import member_behind_run_task
from oc8.knowledge.retrieval import granted_kb_ids, lending_departments, retrieve_kb_context
from oc8.memory.router import retrieve_context
from oc8.modelrouter import NeutralMessage
from oc8.modelrouter.types import ImagePart, TextPart
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from oc8.skills.runtime import LoadedSkill, catalog_block, load_assigned_skills

logger = logging.getLogger(__name__)

_ORIGIN_LABELS: dict[str, str] = {
    "chat": "chat",
    "cron": "schedule",
    "decision": "decision follow-up",
    "webhook": "webhook trigger",
    "event": "trigger",
    "manual": "manual run",
    "handoff": "handoff",
}


async def _origin_label(db: AsyncSession, *, run: m.AgentRun | None) -> str:
    """A2 "Origin" line. `run.source` is the ck_agent_run_source CHECK
    constraint's enum (manual/cron/event/webhook/delegation/decision/
    handoff/chat). "delegation" gets a best-effort parent-agent name;
    everything else is a flat label. A run_id-less call (e.g. a fixture
    that builds Agent/Department/McpConnection directly and calls
    run_agent with no AgentRun row at all) defaults to "manual run" --
    there is no run to introspect, and a direct invocation is, mechanically,
    the same shape as a manual one."""
    if run is None:
        return "manual run"
    if run.source == "delegation":
        task = await db.get(m.Task, run.task_id) if run.task_id else None
        parent_task = (
            await db.get(m.Task, task.parent_task_id) if task and task.parent_task_id else None
        )
        parent_agent = (
            await db.get(m.Agent, parent_task.assigned_agent_id)
            if parent_task and parent_task.assigned_agent_id
            else None
        )
        if parent_agent is not None:
            return f"delegation from {parent_agent.name}"
        return "delegation"
    return _ORIGIN_LABELS.get(run.source, run.source)


def system_prompt(
    agent: m.Agent,
    *,
    caps: ModelCaps,
    tenant_name: str,
    pinned: Mapping[str, Any] | None = None,
) -> str:
    """`pinned` is the run's resolved version (`resolve_version`); when given,
    role title and mission come from it rather than the live row, so a
    mid-run edit never rewrites the instructions a run is working to. `name`
    and `presentation` (guardrails) are identity/operational state, not
    versioned -- see `render_system_prompt`'s own docstring."""
    return render_system_prompt(agent, caps=caps, tenant_name=tenant_name, pinned=pinned)


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
    #: The Copilot only: whether a member is behind this turn (not a wake-up
    #: after a delegated sub-task, nor an operator in a colleague's session).
    #: Without one, `memory_write` has nobody to note for and is not offered.
    copilot_has_member: bool = False
    #: Whether the agent produces at least one live board: `board_write` is offered
    #: to it alone (see oc8.boards.mission).
    produces_board: bool = False
    #: Whether `web_search` is offered: the turn asked for the web and the
    #: tenant has a provider (`oc8.websearch.service.offered`).
    has_web_search: bool = False
    #: The tenant's resolved timezone label (resolve_timezone's first return
    #: value) -- read back by engine.py/internal_agent.py after this call so
    #: C3's step stamp (format_step_stamp) uses the SAME resolved zone as A2's
    #: "Now" line, rather than re-resolving it a second time.
    tz: str = "UTC"


@dataclass(frozen=True)
class Resume:
    """A chat turn's leg woken by a decision (a held call's approval, an
    offer's answer): the conversation and the turn stay, and the leg is told
    what it already did and what was decided after them."""

    #: This run's own work before the pause (`chat.digest`), as the
    #: assistant's turn; None reads as "[no tool calls yet]".
    own_work_digest: str | None
    #: What the leg is told to do now, as the user's turn.
    instruction: str


#: Where a figure has to come from, in a chat. A budget, a usage figure or a
#: record count the model remembers from an earlier turn, or makes up, reads
#: exactly like one it just looked up; said once, in the stable system prefix,
#: so it costs nothing per turn. The answer itself is never rewritten. Only
#: for chat turns (and the Copilot): a triggered run's figures come with its
#: task, and an earlier turn is what a chat has and a run does not.
GROUNDING_RULE = (
    "FIGURES: numbers about budgets, usage, runs and business records must come "
    "from this turn -- a tool result, the knowledge context provided, the task "
    "or the person's own message -- never from memory or what an earlier turn "
    "recalled. If none of these holds the figure, say so instead of estimating it."
)


#: The assistant's turn of a resumed leg whose run had called no tool yet.
NO_TOOL_CALLS_YET = "[no tool calls yet]"


#: Said on a turn that offers `web_search` (spec §10): its queries leave the
#: organisation, so they must never carry what the person's data says.
WEB_SEARCH_NOTE = (
    "WEB: web search is on for this turn -- use web_search for current or "
    "public facts, search with public terms only (never names, numbers or text "
    "from records, chats or files), and cite each web result you rely on by "
    "its number, e.g. [1]."
)


#: How many tool keys one roster line names before it says "…".
ROSTER_TOOL_LIMIT = 8


async def _published_narrowings(
    db: AsyncSession, agents: list[m.Agent]
) -> dict[uuid.UUID, Mapping[str, Any]]:
    """Each agent's PUBLISHED narrowing (`resolve_version`'s source, a draft
    grants nothing), in one query for the whole roster. An agent never
    published, or a version that predates the column, reads its row -- the
    same fallback `resolve_version` takes."""
    from oc8.agents.versioning import version_payload

    version_ids = [a.current_version_id for a in agents if a.current_version_id is not None]
    versions: dict[uuid.UUID, m.AgentVersion] = {}
    if version_ids:
        versions = {
            v.id: v
            for v in (
                await db.execute(select(m.AgentVersion).where(m.AgentVersion.id.in_(version_ids)))
            ).scalars()
        }
    out: dict[uuid.UUID, Mapping[str, Any]] = {}
    for a in agents:
        version = versions.get(a.current_version_id) if a.current_version_id else None
        payload = version_payload(version) if version is not None else {}
        out[a.id] = (payload["narrowing"] if "narrowing" in payload else a.narrowing) or {}
    return out


def _tools_note(department: m.Department, narrowing: Mapping[str, Any]) -> str:
    """` — tools: a, b` -- the tool keys enabled for the agent (its
    department's frame intersected with its narrowing), sorted, at most
    `ROSTER_TOOL_LIMIT`. Keys, not labels: the model calls tools by key."""
    policies = effective_tool_policies(dict(department.frame or {}), dict(narrowing))
    keys = sorted(key for key, policy in policies.items() if policy.enabled)
    if not keys:
        return ""
    shown = ", ".join(keys[:ROSTER_TOOL_LIMIT])
    return f" — tools: {shown}" + (", …" if len(keys) > ROSTER_TOOL_LIMIT else "")


async def roster_block(
    db: AsyncSession,
    *,
    agent: m.Agent,
    task: m.Task | None = None,
    run: m.AgentRun | None = None,
) -> str | None:
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

    On a turn that acts for a person (`copilot_person` behind `task`, on any
    door) it names only the agents that person may run -- exactly what
    `_delegate` lets through --
    and every line says which tools the agent has, so the Copilot can offer an
    agent that already does the job instead of doing it itself.
    """
    if agent.is_tenant_assistant:
        from oc8.copilot.agent_match import member_run_reach, runnable_with

        result = (
            await db.execute(
                select(m.Agent, m.Department)
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
        rows: list[tuple[m.Agent, m.Department]] = [(a, dept) for a, dept in result]
        person = await copilot_person(db, tenant_id=agent.tenant_id, task=task)
        if person is not None:
            reach = await member_run_reach(
                db,
                member_id=person.id,
                tenant_id=agent.tenant_id,
                token_role=await person_token_role(
                    db,
                    tenant_id=agent.tenant_id,
                    run_id=run.id if run is not None else None,
                    person=person,
                ),
            )
            rows = [(a, dept) for a, dept in rows if runnable_with(reach, a)]
        else:
            # Nobody behind the turn: `_delegate` crosses no department then
            # (`_member_may_reach_department` fails closed), and a personal
            # agent answers to its owner alone -- never name one.
            rows = [(a, dept) for a, dept in rows if dept.kind != DEPARTMENT_KIND_PERSONAL]
        if not rows:
            return None
        narrowings = await _published_narrowings(db, [a for a, _ in rows])
        lines = [
            f"- {a.id}: {a.name} ({dept.name}"
            + (f", {a.role_title})" if a.role_title else ")")
            + _tools_note(dept, narrowings[a.id])
            for a, dept in rows
        ]
        if person is not None:
            return (
                "Every agent the person you are acting for may run, by department -- "
                "delegate_task may reach any of them:\n" + "\n".join(lines)
            )
        return (
            "Every agent in this tenant, by department -- delegate_task may reach any "
            "of them if the person you are acting for can:\n" + "\n".join(lines)
        )

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
    actor: AgentActor | None,
) -> frozenset[str]:
    """Which of the 5 status tools' permissions this run's Assistant may
    offer, mirroring `require_departmental`'s admission formula
    (`api/deps.py:282-359`) off-request, for the human behind this chat.

    Only ever non-empty for the tenant Assistant with a real chat-driven
    task behind it -- every other agent, and every non-chat origin
    (delegated/scheduled runs have no chat session), gets the empty set,
    which is exactly what `offered_tools` needs to withhold all 5 tools.

    `actor` is `build_run_preamble`'s own already-resolved
    `_resolve_agent_actor` call (for A2's "Acting for" line) passed straight
    through -- both need the identical `(tenant_id, task, run_id)` lookup, so
    resolving it twice per run would be a pointless duplicate query.
    """
    if not agent.is_tenant_assistant or task is None:
        return frozenset()
    agent_actor = actor
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
        if permission in authority.tenant_wide or agent_actor.scope.holds_anywhere(permission):
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
    caps: ModelCaps,
    max_steps: int,
    task_images: list[ImagePart] | None = None,
    supports_vision: bool = False,
    task: m.Task | None = None,
    run_id: uuid.UUID | None = None,
    pinned: Mapping[str, Any] | None = None,
    replay: ChatReplay | None = None,
    resume: Resume | None = None,
) -> RunPreamble:
    """Seed a run's conversation: system context first, the task last.

    Message order is part of the contract -- the task must be the final turn, so
    the model reads its instructions against context already established.

    `pinned` is the run's resolved agent version (`resolve_version`). Every
    runtime passes it; None (a direct call with no run) reads the live row.

    `replay` (a chat turn of the in-process engine, spec §5.2): the system
    messages keep only what is stable for the session, the earlier turns
    follow as real turns, and everything that varies per turn -- run context,
    memory and KB retrieval, roster, direct access -- goes into one final
    user message with the new turn. A replay that cannot be read falls back
    to the flattened transcript in today's order.

    `resume` (that chat turn's leg after a decision): two more turns after
    the final user message -- the run's own work so far as the assistant's,
    the decision as the user's -- so the cached prefix stays as it was.
    """
    org = await db.get(m.Organization, tenant_id)
    tenant_name = org.name if org is not None else "the organization"

    tz_label, tz = resolve_timezone(org.timezone if org is not None else "UTC")
    now = dt.datetime.now(tz)

    # _resolve_agent_actor requires a real Task (it dereferences task.id); a
    # delegated/scheduled run passes task=None, in which case there is no
    # chat-driven human behind this run at all, the same fail-closed shape as
    # _gated_copilot_permissions' own guard just below.
    actor = (
        await _resolve_agent_actor(db, tenant_id=tenant_id, task=task, run_id=run_id)
        if task is not None
        else None
    )
    if actor is not None:
        acting_for = actor.member.display_name or actor.member.subject
    else:
        acting_for = "scheduled run, no acting person"

    run = await db.get(m.AgentRun, run_id) if run_id is not None else None
    origin = await _origin_label(db, run=run)

    turns: list[NeutralMessage] | None = None
    query_text = task_text
    replay_restricted = False
    if replay is not None:
        try:
            # A savepoint, so a failed read leaves the run's transaction usable.
            async with db.begin_nested():
                path = await replay_path(db, replay=replay)
                # Bounded by the window (spec §5.3): from the newest
                # checkpoint, with one made now when the chat outgrew it.
                turns = await replay_messages(
                    db,
                    replay=replay,
                    model_locality=model_locality,
                    path=path,
                    window=caps.context_window_tokens,
                )
        except Exception:
            logger.warning(
                "chat replay for run %s failed; using the flattened transcript",
                run_id,
                exc_info=True,
            )
            turns = None
            if run is not None:
                try:
                    task_text, replay_restricted = await flattened_task_text_for_model(
                        db, run, model_locality=model_locality
                    )
                except Exception:
                    # Never fail the turn over the fallback: the task text
                    # passed in is the new turn alone.
                    logger.warning(
                        "flattened transcript for run %s failed; keeping the task text",
                        run_id,
                        exc_info=True,
                    )
            query_text = task_text
        else:
            task_text = new_turn_text((run.context or {}) if run is not None else {}, task_text)
            answer = last_answer(path, model_locality=model_locality)
            query_text = task_text if answer is None else f"{task_text}\n{answer}"
            # A restricted answer replayed in full (a local model) puts
            # restricted data into this run, as a restricted KB hit does.
            replay_restricted = any(
                msg.restricted and not withheld(msg, model_locality=model_locality) for msg in path
            )
    department = await db.get(m.Department, agent.department_id)
    department_name = department.name if department is not None else "unassigned"

    # The systems whose tools can actually be offered (authz.pdp.offers_tool),
    # not every key the frame names: a connection switched off for the
    # department or the agent, or granting nothing, is not reachable, and
    # naming it sends the model looking for tools it never gets.
    narrowing = (pinned["narrowing"] or {}) if pinned is not None else (agent.narrowing or {})
    connection_names = reachable_connections(effective_tool_policies(frame, narrowing))
    connection_details: dict[str, str] = {}
    if connection_names:
        # The tool's one row, whichever department the agent sits in (the
        # policy above decided it reaches it).
        rows = [
            row
            for row in await tool_connections(
                db, names=connection_names, department_id=agent.department_id, connected_only=False
            )
            # A custom server in no department reaches no agent run.
            if not needs_department(row)
        ]
        for row in rows:
            cfg = row.config if isinstance(row.config, dict) else {}
            instruction = cfg.get("server_instructions")
            if isinstance(instruction, str) and instruction:
                connection_details[row.name] = instruction

    messages: list[NeutralMessage] = [
        NeutralMessage(
            role="system",
            content=system_prompt(agent, caps=caps, tenant_name=tenant_name, pinned=pinned),
        )
    ]
    # Said once, before anything a stranger wrote can arrive. Every answer a
    # connection returns is fenced as <external>, and this is what makes that
    # fence mean something to the model. It is the cheap half of the defence --
    # the half that holds without the model's cooperation is the blast-radius
    # limit in agent/blast_radius.py.
    messages.append(NeutralMessage(role="system", content=PROVENANCE_RULE))
    if agent.is_tenant_assistant or (run is not None and run.source == "chat"):
        messages.append(NeutralMessage(role="system", content=GROUNDING_RULE))

    # Per-turn context: system messages without a replay; with one, part of
    # the final user message, so the system prefix stays stable across turns.
    turn_blocks: list[str] = []

    def _per_turn(block: str) -> None:
        if turns is not None:
            turn_blocks.append(block)
        else:
            messages.append(NeutralMessage(role="system", content=block))

    copilot_member = (
        await member_behind_run_task(db, tenant_id=tenant_id, task=task, run_id=run_id)
        if agent.is_tenant_assistant
        else None
    )
    memory_ctx = await retrieve_context(
        db,
        agent=agent,
        tenant_id=tenant_id,
        frame=frame,
        query_text=query_text,
        model_locality=model_locality,
        narrowing=(pinned["narrowing"] or {}) if pinned is not None else None,
        member_id=copilot_member,
    )
    if memory_ctx:
        _per_turn(memory_ctx)

    # Resolved once for this run (spec A5): what the up-front retrieval reads
    # and what `has_knowledge` offers search_knowledge for cannot disagree.
    lenders = await lending_departments(
        db, agent=agent, owner_claim=_owner_claim(db, tenant_id=tenant_id, run_id=run_id)
    )
    kb_ctx, contains_restricted = await retrieve_kb_context(
        db,
        agent=agent,
        tenant_id=tenant_id,
        query_text=query_text,
        frame=frame,
        model_locality=model_locality,
        lenders=lenders,
    )
    if kb_ctx:
        _per_turn(kb_ctx)
    contains_restricted = contains_restricted or replay_restricted
    has_knowledge = bool(await granted_kb_ids(db, agent=agent, lenders=lenders))

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
        roster = await roster_block(db, agent=agent, task=task, run=run)
        if roster is not None:
            _per_turn(roster)

    if agent.is_tenant_assistant:
        # What the Copilot may offer to do itself (copilot direct access §3).
        from oc8.copilot.direct_access import direct_access_block

        block = await direct_access_block(
            db,
            run=run,
            agent=agent,
            narrowing=(pinned["narrowing"] or {}) if pinned is not None else None,
        )
        if block is not None:
            _per_turn(block)

    from oc8.websearch.service import offered as web_search_offered

    has_web_search = run is not None and await web_search_offered(
        db, tenant_id=tenant_id, context=run.context
    )
    if has_web_search:
        # Per turn: "Web" is switched on message by message.
        _per_turn(WEB_SEARCH_NOTE)

    from oc8.boards.mission import board_section

    boards_text = None if agent.is_tenant_assistant else await board_section(db, agent=agent)
    if boards_text is not None:
        messages.append(NeutralMessage(role="system", content=boards_text))

    assigned_skills = await load_assigned_skills(db, agent=agent, tenant_id=tenant_id)
    catalog = catalog_block(assigned_skills)
    if catalog:
        messages.append(NeutralMessage(role="system", content=catalog))

    run_context = run_context_block(
        now=now,
        tz_label=tz_label,
        acting_for=acting_for,
        origin=origin,
        department=department_name,
        connection_names=connection_names,
        max_steps=max_steps,
        instruction_file_count=instruction_file_count,
        task_attachment_count=len(task_images) if task_images else 0,
        connection_details=connection_details or None,
    )
    if turns is not None:
        # Cache breakpoints (spec §5.2): the end of the stable system prefix,
        # and the end of the replayed turns, which only grow turn by turn.
        messages[-1].cache_breakpoint = True
        if turns:
            turns[-1].cache_breakpoint = True
        messages.extend(turns)
        # One final user message: run context, per-turn blocks, the new turn.
        final_text = "\n\n".join([run_context, *turn_blocks, task_text])
    else:
        messages.append(NeutralMessage(role="user", content=run_context))
        final_text = task_text

    if task_images and supports_vision:
        messages.append(
            NeutralMessage(role="user", content=[TextPart(text=final_text), *task_images])
        )
    else:
        note = ""
        if task_images and not supports_vision:
            note = (
                "\n\n(An image was attached to this message, but this agent's "
                "model cannot process images.)"
            )
        messages.append(NeutralMessage(role="user", content=final_text + note))
    if resume is not None:
        messages.append(
            NeutralMessage(role="assistant", content=resume.own_work_digest or NO_TOOL_CALLS_YET)
        )
        messages.append(NeutralMessage(role="user", content=resume.instruction))

    copilot_permissions = await _gated_copilot_permissions(
        db, agent=agent, tenant_id=tenant_id, task=task, run_id=run_id, actor=actor
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
        copilot_has_member=copilot_member is not None,
        produces_board=boards_text is not None,
        has_web_search=has_web_search,
        tz=tz_label,
    )
