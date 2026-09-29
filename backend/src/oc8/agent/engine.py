"""Agent execution loop (tech-spec §8.3).

trigger -> assemble context -> LLM complete (via Model Router) -> for each tool
call: PEP authorize -> invoke MCP tool -> feed result back -> repeat until the
model stops. Every tool call is audited; token usage is metered; a threshold
breach raises a HITL approval and suspends the run.

Shared stages (authorisation, record guards, result shaping, completion
gating) live in `oc8.agent.harness`; this module is the in-process driver of
that pipeline, `api/v1/internal_agent.py` the isolated one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent import cache_flow
from oc8.agent.control_tools import (
    CONTROL_TOOL_NAMES,
    FIND_TOOLS,
    MAX_DELEGATION_DEPTH,  # re-exported for tests/coding/test_engine_delegation.py
    execute_control_tool,
    offered_tools,
)
from oc8.agent.elicitation import ElicitationNeeded, arguments_with_answer
from oc8.agent.harness import GateVerdict, Harness, resolve_caps
from oc8.agent.harness.calls import (
    call_sig as _call_sig,  # re-exported for mcp_gateway.py and older tests
)
from oc8.agent.harness.procedures import (
    newly_satisfied_lines,
    procedure_haystack,
    satisfied_ids,
)
from oc8.agent.harness.prompts import compaction_instruction
from oc8.agent.harness.retrieval import select_completion_tools
from oc8.agent.harness.stages.a_compaction import (
    already_compacted_this_step,
    prompt_token_fallback,
    rebuild_transcript,
    should_compact,
)
from oc8.agent.harness.stages.a_masking import mask_observations
from oc8.agent.harness.stages.b_approval import autonomy_of, strip_justification
from oc8.agent.harness.stages.b_authorize import (
    authorize as _authorize,  # re-exported for mcp_gateway.py and older tests
)
from oc8.agent.harness.stages.b_blast_radius import check_blast_radius
from oc8.agent.harness.stages.b_claims import claim_write
from oc8.agent.harness.stages.b_clarify import (
    apply_clarification,
    clarification_prompt,
    parse_clarification,
    should_clarify,
)
from oc8.agent.harness.stages.b_idempotency import record_for, replay_for
from oc8.agent.harness.stages.b_outward import check_outward, remember_outward
from oc8.agent.harness.stages.b_read_before_write import note_access
from oc8.agent.harness.stages.b_risk_tier import classify_tier
from oc8.agent.harness.stages.c_errors import ToolError, classify_exception
from oc8.agent.harness.stages.c_ledger import (
    ledger_fingerprint,
    record_decision,
    record_file,
    record_outward,
    record_tool,
    render_ledger_block,
)
from oc8.agent.harness.stages.c_reminders import track_repeat_tool_call
from oc8.agent.harness.stages.c_spill import persist_spill
from oc8.agent.harness.step_timing import finish_step, note_model, note_tools, start_step
from oc8.agent.mcp_client import open_tool_session, resolve_auth_header
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.agent.mcp_requirements import wrap_with_requirements
from oc8.agent.offering import (
    allowed_connections_for_skills,
    connection_by_tool,
    unavailable_sentence,
)
from oc8.agent.outward import outward_target
from oc8.agent.preamble import build_run_preamble
from oc8.agent.tool_notes import apply_tool_notes
from oc8.agent.tool_routing import RoutedToolset
from oc8.agent.tool_semantics import describe_focus, describes_a_record, record_identity
from oc8.agents.versioning import pinned_model_config_id, resolve_version
from oc8.approvals import raise_approval
from oc8.audit import append_event
from oc8.authz.pdp import Decision, Effect, effective_tool_policies, required_right
from oc8.capas.claude_hooks import dispatch_claude_event
from oc8.capas.claude_hooks.context import (
    base_payload,
    session_start,
    tool_event,
    tool_result,
    user_prompt_submit,
)
from oc8.capas.claude_hooks.context import (
    task_created as claude_task_created,
)
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.coding.tools import CODING_FRAME_KEY, CODING_TOOL_RIGHTS, Toolset
from oc8.config import get_settings
from oc8.hooks.bus import dispatch_filter
from oc8.hooks.executor import InProcessExecutor
from oc8.hooks.types import HookCtx
from oc8.memory.router import write_memory
from oc8.metering import check_budget, record_usage, trigger_budget_hard_stop
from oc8.modelrouter import (
    NeutralMessage,
    NeutralTool,
    ToolCall,
    get_model_router,
    locality_for_provider,
    stream_completion_with_fallback,
)
from oc8.modelrouter.accumulate import StreamTiming, accumulate_stream
from oc8.modelrouter.auto_router import (
    AutoRouterError,
    cascade_should_escalate,
    cascade_verify_enabled,
    escalate_auto_router,
    is_auto_config,
    record_preference_label,
    remember_agent_route,
    resolve_auto_config,
    tier_map,
)
from oc8.modelrouter.keys import resolve_model_base_url
from oc8.modelrouter.sampling import bumped_for_length_retry, resolve_params
from oc8.modelrouter.trim import overflow_tokens
from oc8.modelrouter.types import ImagePart, ModelParams, with_prompt_cache_key
from oc8.observability import get_tracer, record_budget_exceeded, record_tool_call
from oc8.realtime.emit import (
    note_focus,
    publish_agent_status,
    publish_run_step_timing,
    publish_run_token_delta,
    publish_run_tool_call,
    record_activity,
)
from oc8.runtime.run_context import append_tool_call
from oc8.runtime.step_record import call_state_for
from oc8.runtime.supervision_hook import maybe_checkpoint, maybe_create_anchor
from oc8.skills.runtime import (
    LoadedSkill,
    instruction_block,
)
from oc8.storage import s3

# mypy's no_implicit_reexport (strict mode) otherwise treats these three
# renamed re-exports as private to this module; mcp_gateway.py and
# tests/coding/test_engine_delegation.py import them from here directly.
__all__ = [
    "MAX_DELEGATION_DEPTH",
    "_authorize",
    "_call_sig",
]

logger = logging.getLogger(__name__)


def _active_procedures(
    skills: list[LoadedSkill],
) -> list[tuple[str, str, tuple]]:
    return [
        (s.definition.slug or s.tool_name, s.name, s.definition.steps)
        for s in skills
        if s.definition.steps
    ]


def _procedure_texts(skills: list[LoadedSkill]) -> list[str]:
    return [procedure_haystack(s.definition.steps) for s in skills if s.definition.steps]


def _satisfied_map(
    procedures: list[tuple[str, str, tuple]],
    harness: Harness,
) -> dict[str, frozenset[str]]:
    return {
        slug: satisfied_ids(steps, harness.state.ledger, harness.state.procedure.get(slug))
        for slug, _name, steps in procedures
    }


def _instruction_for(skill: LoadedSkill, harness: Harness) -> str:
    if not skill.definition.steps:
        return instruction_block(skill)
    slug = skill.definition.slug or skill.tool_name
    done = satisfied_ids(
        skill.definition.steps,
        harness.state.ledger,
        harness.state.procedure.get(slug),
    )
    return instruction_block(skill, done)


def _max_steps(definition: dict[str, Any] | None) -> int:
    """Step budget for a run: an agent plugin may raise it per agent for longer,
    multi-record workflows; otherwise the framework setting applies.

    Takes the run's PINNED definition (`resolve_version(...)["definition"]`),
    not the agent row, so every runtime bounds a run by the budget of the
    version it started with."""
    override = (definition or {}).get("max_steps")
    if isinstance(override, int) and override > 0:
        return override
    return max(1, get_settings().agent_max_steps)


CancelCheck = Callable[[], Awaitable[bool]]
# Returns operator messages addressed to a running agent since the last poll.
InboxCheck = Callable[[], Awaitable[list[str]]]


@dataclass
class RunResult:
    task_id: uuid.UUID
    agent_id: uuid.UUID
    status: str  # done | waiting_for_approval | waiting_for_input | failed | budget_exceeded
    output: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    # AgentRun rows this run created (via delegate_task, §7) that the caller
    # must PUBLISH after committing. run_agent must never publish them itself:
    # it holds a transaction whose tenant binding is transaction-local, and the
    # worker could read a run whose row isn't durably visible yet.
    pending_runs: list[uuid.UUID] = field(default_factory=list)
    # Every render_component call this run made, in call order -- see the
    # accumulator's own comment in run_agent for why this must be durable.
    rendered_components: list[dict[str, Any]] = field(default_factory=list)
    # The agent's current to-do list (todo_write), whole-list-replace: this is
    # the LATEST call's list, not a log of every call. Empty means the tool was
    # never called this run, not that every item finished.
    todos: list[dict[str, str]] = field(default_factory=list)
    # One latency record per model step (see harness.step_timing). Empty on
    # runs that never entered the step loop (e.g. budget gate).
    step_timings: list[dict] = field(default_factory=list)


def _json_chunks(text: str) -> list[str]:
    """Yield balanced [...] / {...} substrings from free text."""
    chunks: list[str] = []
    opens = {"[": "]", "{": "}"}
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in opens:
            depth = 0
            for j in range(i, len(text)):
                if text[j] in opens:
                    depth += 1
                elif text[j] in opens.values():
                    depth -= 1
                    if depth == 0:
                        chunks.append(text[i : j + 1])
                        i = j
                        break
        i += 1
    return chunks


def _salvage_tool_calls(text: str, tools: list[NeutralTool]) -> list[ToolCall]:
    """Weak tool-calling models sometimes write the call as JSON text instead of
    emitting a real tool call. Recover those so the run still executes."""
    names = {t.name for t in tools}
    if not names:
        return []
    calls: list[ToolCall] = []
    for chunk in _json_chunks(text):
        try:
            data = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or item.get("name") not in names:
                continue
            args = item.get("arguments") or item.get("parameters") or {}
            if isinstance(args, dict):
                calls.append(
                    ToolCall(id=f"salvaged_{len(calls)}", name=item["name"], arguments=args)
                )
    return calls


async def open_run_task(
    db: AsyncSession,
    *,
    agent: m.Agent,
    task_text: str,
    tenant_id: uuid.UUID,
    parent_task_id: uuid.UUID | None = None,
    delegation_depth: int = 0,
    resume_task_id: uuid.UUID | None = None,
) -> m.Task:
    """Open the Task row a run works on, and flush it so it has an id.

    Every runtime must go through this: the task is what an approval, a
    delegation and the office view all hang off, so a run without one is a run
    whose approval can never be resolved back to it.

    A run that already has a task (``resume_task_id``) is RESUMING a suspended
    leg -- after an approval or a clarification -- and continues that same task
    rather than opening a second one. One suspend/resume cycle is one unit of
    work: opening a new task instead would strand the suspended one in
    waiting_for_approval forever (nothing ever revisits it), so every approval
    would leave a permanent ghost on the board, and the new task would be
    titled with the internal resume instruction rather than what was asked.
    """
    if resume_task_id is not None:
        resumed = await db.get(m.Task, resume_task_id)
        if resumed is not None:
            resumed.state = "in_progress"
            await db.flush()
            return resumed

    task_fields: dict[str, Any] = {
        "department_id": agent.department_id,
        "assigned_agent_id": agent.id,
        "title": task_text[:200],
        "state": "in_progress",
        "created_by": agent.id,
    }
    task_fields = await dispatch_filter(
        HookCtx(tenant_id=tenant_id),
        "task.before_create",
        task_fields,
        executor_default=InProcessExecutor(),
    )
    task = m.Task(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        department_id=task_fields["department_id"],
        assigned_agent_id=task_fields["assigned_agent_id"],
        title=task_fields["title"],
        state=task_fields["state"],
        created_by=task_fields["created_by"],
        # Deliberately NOT routed through the `task.before_create` hook filter
        # above: delegation_depth is the loop bound, so a plugin must not be
        # able to reset it to 0 and defeat it.
        parent_task_id=parent_task_id,
        delegation_depth=delegation_depth,
    )
    db.add(task)
    await db.flush()
    await dispatch_claude_event(
        tenant_id,
        "TaskCreated",
        claude_task_created(
            title=task.title,
            tenant_id=tenant_id,
            agent_id=agent.id,
            task_id=task.id,
        ),
    )
    return task


@dataclass(frozen=True)
class _McpAuth:
    connection_key: str
    tool_scopes: dict[str, Any] | None
    value_spec: dict[str, Any] | None
    focus_spec: dict[str, Any] | None
    outward_tools: list[str] | None
    guardrail_attribute_specs: list[dict[str, Any]]


def _mcp_auth(mcp_conn: m.McpConnection) -> _McpAuth:
    """Frame-check inputs for one connection -- scopes, value, focus, attributes."""
    cfg = mcp_conn.config if isinstance(mcp_conn.config, dict) else {}
    # The read/write/send classification `required_right` needs lives
    # on the manifest's own ToolPackConnection, not this row's
    # `scopes` column -- that column is an unrelated, list-shaped
    # field (see `resolve_tool_pack_connection`'s docstring). Reading
    # it here used to fail closed to "write" for every tool call
    # whenever the row's `scopes` wasn't itself a dict, which is the
    # common case.
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is not None and isinstance(manifest_conn.scopes, dict):
        tool_scopes: dict[str, Any] | None = manifest_conn.scopes
    elif isinstance(mcp_conn.scopes, dict):
        # A connection with no manifest (plugin removed from disk, or
        # never plugin-backed at all) that still carries an operator-
        # supplied dict on the row itself -- `CreateMcpConnectionRequest`
        # allows this. Kept as a fallback, not the primary path.
        tool_scopes = mcp_conn.scopes
    else:
        tool_scopes = None
    vs = cfg.get("value_spec")
    fs = cfg.get("focus_spec")
    ot = cfg.get("outward_tools")
    return _McpAuth(
        connection_key=mcp_conn.name,
        tool_scopes=tool_scopes,
        value_spec=vs if isinstance(vs, dict) else None,
        focus_spec=fs if isinstance(fs, dict) else None,
        outward_tools=ot if isinstance(ot, list) else None,
        guardrail_attribute_specs=(
            [
                {
                    "key": a.key,
                    "datatype": a.datatype,
                    "tools": a.tools,
                    "extract": a.extract,
                }
                for a in manifest_conn.guardrail_attributes
            ]
            if manifest_conn is not None
            else []
        ),
    )


async def _open_mcp_session(
    db: AsyncSession, *, tenant_id: uuid.UUID, mcp_conn: m.McpConnection
) -> Any:
    cfg = mcp_conn.config if isinstance(mcp_conn.config, dict) else {}
    env = await resolve_mcp_env(db, tenant_id=tenant_id, cfg=cfg, connection_name=mcp_conn.name)
    headers = resolve_auth_header(cfg, env)
    if mcp_conn.transport == "manual_http":
        return await open_tool_session(
            transport="manual_http",
            server_url=mcp_conn.server_url,
            http_tools=list(cfg.get("http_tools", [])),
            headers=headers,
        )
    command, args = wrap_with_requirements(cfg.get("command", ""), cfg.get("args", []), cfg)
    return await open_tool_session(
        transport=mcp_conn.transport,
        command=command,
        args=args,
        server_url=mcp_conn.server_url,
        headers=headers,
        env=env,
    )


async def run_agent(
    db: AsyncSession,
    *,
    agent: m.Agent,
    task_text: str,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    mcp_conn: m.McpConnection | None = None,
    mcp_conns: Sequence[m.McpConnection] | None = None,
    toolset: Toolset | None = None,
    parent_task_id: uuid.UUID | None = None,
    delegation_depth: int = 0,
    cancel_check: CancelCheck | None = None,
    inbox_check: InboxCheck | None = None,
    pre_decided: dict[str, str] | None = None,
    originating_operator: str | None = None,
    task_images_raw: list[dict[str, str]] | None = None,
) -> RunResult:
    async def _run() -> RunResult:
        settings = get_settings()
        router = get_model_router()

        # A resume leg continues the task its suspended leg opened; see
        # open_run_task. The run is the only place that link is recorded, so a
        # runtime that gets no run_id (a direct run_agent call in a test) simply
        # opens a fresh task, as before. The same row also carries stamped
        # pins (`mcp_connection_ids`) so an in-process run sees every login
        # the executor resolved, not just the first -- and the agent version
        # this run was pinned to at intake.
        run_row: m.AgentRun | None = (
            await db.get(m.AgentRun, run_id) if run_id is not None else None
        )
        # Every behavioural field below (model, narrowing, definition, mission,
        # team-lead flag) comes from the run's pinned version, never the live
        # row -- the same answer the isolated control plane and the MCP gateway
        # give for the same run. `department.frame` stays a live read on
        # purpose: it is the tenant's ceiling and must bite mid-run.
        pinned = await resolve_version(db, run_row, agent)
        pinned_model_id = pinned_model_config_id(pinned)

        model_config: m.ModelConfig | None = None
        if pinned_model_id is not None:
            model_config = await db.get(m.ModelConfig, pinned_model_id)
        # Virtual Auto ModelConfig: keep the policy row and resolve a concrete
        # target before each completion (session latch + one-way escalation).
        auto_cfg: m.ModelConfig | None = (
            model_config if is_auto_config(model_config) else None
        )
        cascade_flag = {"attempted": False, "escalated": False}
        # Mutated in place each step so nested completions see an escalation
        # without rebinding the names they close over.
        active_route: dict[str, Any] = {}
        if model_config is not None and auto_cfg is None:
            provider = model_config.provider
            model = model_config.model
            model_locality = model_config.locality
        elif auto_cfg is not None:
            provider = auto_cfg.provider
            model = auto_cfg.model
            model_locality = auto_cfg.locality
        else:
            provider = (agent.presentation or {}).get("provider", settings.default_model_provider)
            model = settings.default_model
            model_locality = locality_for_provider(provider)
        # Gate on the CONFIGURED model, not the provider generally -- an
        # agent's model_config is what actually receives the completion
        # request, so that is what decides whether an attached image can be
        # sent along with it. Resolved here, right alongside the same
        # model_config lookup, rather than deep inside build_run_preamble.
        if auto_cfg is not None:
            supports_vision = False
            try:
                for cfg_id in tier_map(auto_cfg).values():
                    tier_cfg = await db.get(m.ModelConfig, cfg_id)
                    if tier_cfg is not None and bool(
                        (tier_cfg.params or {}).get("supports_vision", False)
                    ):
                        supports_vision = True
                        break
            except AutoRouterError:
                supports_vision = False
        else:
            supports_vision = (
                bool(model_config.params.get("supports_vision", False))
                if model_config is not None
                else False
            )
        task_images = [
            ImagePart(
                data=await s3.get_object(entry["bucket_key"]),
                content_type=entry["content_type"],
            )
            for entry in (task_images_raw or [])
        ]
        department = await db.get(m.Department, agent.department_id)
        frame: dict[str, Any] = department.frame if department is not None else {}
        tool_policies = effective_tool_policies(frame, pinned["narrowing"] or {})
        extra_conns: list[m.McpConnection] = list(mcp_conns) if mcp_conns else []
        if not extra_conns and run_row is not None:
            raw_ids = run_row.context.get("mcp_connection_ids")
            if isinstance(raw_ids, list):
                for raw in raw_ids:
                    try:
                        loaded = await db.get(m.McpConnection, uuid.UUID(str(raw)))
                    except ValueError:
                        continue
                    if loaded is not None:
                        extra_conns.append(loaded)
        if not extra_conns and mcp_conn is not None:
            extra_conns = [mcp_conn]
        if toolset is not None:
            connection_config: dict[str, Any] = {}
            connection_key: str | None = CODING_FRAME_KEY
            tool_scopes: dict[str, Any] | None = {
                right: [n for n, r in CODING_TOOL_RIGHTS.items() if r == right]
                for right in ("read", "modify")
            }
            value_spec: dict[str, Any] | None = None
            focus_spec: dict[str, Any] | None = None
            outward_tools: list[str] | None = None
            guardrail_attribute_specs: list[dict[str, Any]] = []
        elif extra_conns:
            auth = _mcp_auth(extra_conns[0])
            connection_key = auth.connection_key
            connection_config = (
                extra_conns[0].config if isinstance(extra_conns[0].config, dict) else {}
            )
            tool_scopes = auth.tool_scopes
            value_spec = auth.value_spec
            focus_spec = auth.focus_spec
            outward_tools = auth.outward_tools
            guardrail_attribute_specs = auth.guardrail_attribute_specs
        else:
            connection_config = {}
            connection_key = None
            tool_scopes = None
            value_spec = None
            focus_spec = None
            outward_tools = None
            guardrail_attribute_specs = []
        async def _record_url(
            name: str,
            arguments: dict[str, Any],
            connection_name: str | None = None,
            spec: dict[str, Any] | None = None,
        ) -> str | None:
            # Same address the isolated loop stores: an Odoo form URL or a Jira
            # browse page, built from the connection that owns the tool.
            owner = next((item for item in extra_conns if item.name == connection_name), None)
            if owner is None and len(extra_conns) == 1:
                owner = extra_conns[0]
            if owner is None or owner.name not in ("odoo", "jira"):
                return None
            ident = record_identity(name, arguments, spec if spec is not None else focus_spec)
            if ident is None:
                return None
            cfg = owner.config if isinstance(owner.config, dict) else {}
            env = await resolve_mcp_env(
                db, tenant_id=tenant_id, cfg=cfg, connection_name=owner.name
            )
            from oc8.agent.record_link import record_url_from_env

            return record_url_from_env(owner.name, env, ident)

        resume_task_id: uuid.UUID | None = run_row.task_id if run_row is not None else None
        task = await open_run_task(
            db,
            agent=agent,
            task_text=task_text,
            tenant_id=tenant_id,
            parent_task_id=parent_task_id,
            delegation_depth=delegation_depth,
            resume_task_id=resume_task_id,
        )

        budget = await check_budget(db, tenant_id=tenant_id, department_id=agent.department_id)
        if budget.hard_exceeded:
            record_budget_exceeded("hard")
            task.state = "budget_exceeded"
            # Freeze the whole breaching scope (agent/department/tenant), cancel its
            # queued runs, and raise one budget_incident approval (§15.4 A2). Replaces
            # the old single-agent pause; add/flush only -- the executor commits. The
            # trigger writes the budget audit event itself.
            await trigger_budget_hard_stop(db, tenant_id=tenant_id, breaching_agent=agent)
            await record_activity(
                db,
                tenant_id=tenant_id,
                agent_id=agent.id,
                status="warning",
                message=f"{agent.name} paused: token budget exceeded",
            )
            return RunResult(task.id, agent.id, "budget_exceeded", "Token budget exceeded.", [], 0)
        if budget.soft_exceeded:
            record_budget_exceeded("soft")
            await record_activity(
                db,
                tenant_id=tenant_id,
                agent_id=agent.id,
                status="warning",
                message=f"{agent.name} is approaching its token budget",
            )

        anchor = await maybe_create_anchor(
            db, tenant_id=tenant_id, agent_id=agent.id, task_id=task.id, task_text=task_text
        )

        # Computed once here and reused inside loop() below (a closure
        # variable, since _max_steps is a pure function of the PINNED
        # definition -- resolve_version(...)["definition"], never the live
        # agent row, so a mid-run edit to the step budget can't move the
        # goalposts under a run already in flight) rather than a second,
        # separately-named call to _max_steps -- both the preamble's
        # step-budget line and the loop's own range bound must agree on the
        # same number.
        max_steps = _max_steps(pinned["definition"])

        # Seeded from the shared preamble so an isolated run gets exactly the same
        # context (memory, KB, roster, skills catalog) as this one -- see
        # oc8.agent.preamble.
        preamble = await build_run_preamble(
            db,
            agent=agent,
            tenant_id=tenant_id,
            task_text=task_text,
            frame=frame,
            model_locality=model_locality,
            caps=resolve_caps(model_config.params if model_config is not None else None),
            max_steps=max_steps,
            task_images=task_images,
            supports_vision=supports_vision,
            task=task,
            run_id=run_id,
            pinned=pinned,
        )
        messages: list[NeutralMessage] = list(preamble.messages)
        assigned_skills = preamble.assigned_skills
        skill_tool_names = preamble.skill_tool_names
        contains_restricted = preamble.contains_restricted
        has_knowledge = preamble.has_knowledge
        has_instruction_files = preamble.has_instruction_files
        copilot_permissions = preamble.copilot_permissions
        # C3's step stamp uses the SAME resolved timezone as A2's "Now" line
        # above, instead of re-resolving it -- consumed by the harness.shape()
        # call inside loop() below.
        tz = preamble.tz
        tool_trace: list[dict[str, Any]] = []
        # Sub-runs created by delegate_task. run_agent must not publish them (see
        # _delegate); every return below hands them to execute_run instead.
        pending_runs: list[uuid.UUID] = []
        # Every render_component call this run makes, in order -- durable (see
        # RunResult.rendered_components), unlike the WS-only publish alongside
        # it: an unattended run (chat/cron) has no live viewer to catch that
        # event, so this is the only copy that survives past the moment it fired.
        rendered_components: list[dict[str, Any]] = []
        # The latest todo_write call's list, replaced wholesale on every call
        # (see RunResult.todos) -- not accumulated like rendered_components.
        todos: list[dict[str, str]] = []
        session_state = {"started": False}

        def _hook_ctx(**extra: Any) -> dict[str, Any]:
            return base_payload(
                tenant_id=tenant_id,
                agent_id=agent.id,
                run_id=run_id,
                task_id=task.id,
                **extra,
            )

        async def _live_tool_call(entry: dict[str, Any]) -> None:
            # Persist THEN publish, same order as every other realtime helper
            # in this file (e.g. approval.created below) -- a WS subscriber
            # that reconnects a moment later must find the row already
            # holding what the event just told it about, not a race where
            # the live push arrives before a fresh GET would see it.
            # `run_row` is None for a run_agent call with no run_id (e.g. a
            # test calling this directly) -- nothing to persist or publish
            # to, so this is a no-op rather than an error.
            if run_id is None or run_row is None:
                return
            await append_tool_call(db, run_row, entry)
            await publish_run_tool_call(tenant_id, run_id=run_id, call=entry)

        async def _finish_step_timing(rec: dict[str, Any], tool_wait_ms: int) -> None:
            # note_tools/finish_step are dev's own capture
            # (oc8.agent.harness.step_timing, unchanged here) -- this only
            # adds the live-publish side that capture never had. No DB write
            # of its own, unlike _live_tool_call above: stepTimings has no
            # incremental append path, and this run's own step_timings list
            # already lands durably through the executor's terminal
            # merge_context({"stepTimings": result.step_timings, ...}); this
            # only spares an already-open tab the wait for that reload. Same
            # no-run no-op as _live_tool_call.
            note_tools(rec, tool_wait_ms)
            finish_step(rec)
            if run_id is None:
                return
            await publish_run_step_timing(tenant_id, run_id=run_id, timing=rec)

        async def _live_token_delta(text: str) -> None:
            # No DB write here, unlike _live_tool_call above -- the full text
            # still lands durably once the turn finishes (the transcript
            # entry loop() already appends below), so there is nothing a
            # fresh mid-turn page load would be missing by skipping these
            # fragments. Same no-run no-op as _live_tool_call.
            if run_id is None:
                return
            await publish_run_token_delta(tenant_id, run_id=run_id, text=text)

        startup_unavailable: list[dict[str, str]] = []

        async def loop(tools: list[NeutralTool], server: Toolset | None) -> RunResult:
            nonlocal model_config, provider, model, model_locality
            active_skills: list[LoadedSkill] = []

            def _offered() -> list[NeutralTool]:
                # Shared with the isolated runtime so both offer the same list --
                # see oc8.agent.control_tools.
                return offered_tools(
                    agent,
                    is_team_lead=bool(pinned["is_team_lead"]),
                    assigned_skills=assigned_skills,
                    active_skills=active_skills,
                    mcp_tools=tools,
                    has_knowledge=has_knowledge,
                    has_instruction_files=has_instruction_files,
                    copilot_permissions=copilot_permissions,
                    # The in-process engine is the one runtime with no
                    # /workspace mount of its own -- see offered_tools' docstring.
                    offer_write_output_file=True,
                )

            # Per-run harness state (spec §3.3): the repeat-call tracker and the
            # tool-output budget, in memory for the lifetime of this loop -- a
            # fresh run_agent call (including a resumed/forked run) starts from
            # zero, an accepted heuristic cost rather than a durable counter.
            harness = Harness(
                caps=resolve_caps(model_config.params if model_config is not None else None)
            )
            if startup_unavailable:
                harness.state.unavailable_connections = list(startup_unavailable)
            definition = agent.definition if isinstance(agent.definition, dict) else {}
            autonomy = autonomy_of(definition)
            raw_b5_grants = definition.get("b5_grants")
            b5_grants = raw_b5_grants if isinstance(raw_b5_grants, list) else []

            def _gate(
                tc: ToolCall,
                *,
                scopes: dict[str, Any] | None,
                config: dict[str, Any],
                connection: str | None,
                spec: dict[str, Any] | None,
            ) -> GateVerdict:
                offered = next((tool for tool in _offered() if tool.name == tc.name), None)
                tier = classify_tier(
                    tc.name,
                    scopes=scopes,
                    config=config,
                    annotations=offered.annotations if offered is not None else None,
                    arguments=tc.arguments,
                )
                identity = record_identity(tc.name, tc.arguments, spec)
                return harness.gate(
                    tc,
                    tier=tier,
                    ledger=harness.state.ledger,
                    connection=connection or "oc8",
                    config=config,
                    autonomy=autonomy,
                    granted=tier in b5_grants,
                    record_label=describe_focus(tc.name, tc.arguments, spec) or "",
                    identity=identity,
                    procedures=_active_procedures(active_skills),
                )

            steps = 0
            step_timings: list[dict] = []
            checkpoint_trace_delta: list[dict[str, Any]] = []
            tokens_since_checkpoint = 0
            # max_steps is the outer, already-computed closure variable (see
            # _run() above) -- not recomputed here, so the preamble's step
            # budget and this loop's own bound never drift apart. Todo
            # continuation (formerly tracked here as todo_continue_rounds) now
            # runs through stages/d_todo.py's harness stage.
            for steps in range(1, max_steps + 1):
                # C3: the step stamp on this turn's shaped tool output reads this.
                harness.state.step_no = steps
                if steps == 1 and server is not None and run_id is not None:
                    parked = await db.get(m.AgentRun, run_id)
                    pending = (
                        (parked.context or {}).get("pending_elicitation")
                        if parked is not None and isinstance(parked.context, dict)
                        else None
                    )
                    answers = [
                        item
                        for item in (parked.context or {}).get("clarifications", [])
                        if isinstance(item, dict) and item.get("answer")
                    ] if parked is not None and isinstance(parked.context, dict) else []
                    if (
                        isinstance(pending, dict)
                        and answers
                        and str(pending.get("connection") or "") == (connection_key or "")
                    ):
                        tool_name = str(pending.get("tool") or "")
                        arguments = arguments_with_answer(
                            dict(pending.get("arguments") or {}), str(answers[-1]["answer"])
                        )
                        parked_ctx = dict(parked.context or {})
                        try:
                            finished = await server.call(tool_name, arguments)
                        except ElicitationNeeded as exc:
                            parked_ctx["pending_elicitation"] = {
                                "connection": connection_key or "",
                                "tool": tool_name,
                                "arguments": arguments,
                                "question": exc.message,
                            }
                            parked.context = parked_ctx
                            messages.append(
                                NeutralMessage(
                                    role="user",
                                    content=(
                                        f"The same call {tool_name} still needs an answer:\n"
                                        f"{exc.message}"
                                    ),
                                )
                            )
                            task.state = "waiting_for_input"
                            return RunResult(
                                task.id,
                                agent.id,
                                "waiting_for_input",
                                exc.message,
                                tool_trace,
                                0,
                                pending_runs,
                                rendered_components,
                                todos,
                                step_timings,
                            )
                        except Exception as exc:
                            finished = f"ERROR: {exc}"
                        messages.append(
                            NeutralMessage(
                                role="user",
                                content=(
                                    f"The same call {tool_name} finished with the "
                                    f"operator's answer:\n{finished}"
                                ),
                            )
                        )
                        parked_ctx.pop("pending_elicitation", None)
                        parked.context = parked_ctx
                if not session_state["started"]:
                    await dispatch_claude_event(
                        tenant_id,
                        "SessionStart",
                        session_start(prompt=task_text, **_hook_ctx()),
                    )
                    session_state["started"] = True
                if cancel_check is not None and await cancel_check():
                    # Operator cancelled (§7.2). Stop BEFORE the next model call so no
                    # further tokens are spent. The task genuinely didn't finish, so
                    # it's marked failed; the RUN carries the precise 'interrupted'
                    # state + cancellation_kind. pending_runs is carried out: a run
                    # cancelled after it already delegated must still publish those
                    # legitimately-created sub-runs.
                    task.state = "failed"
                    await maybe_checkpoint(
                        db,
                        tenant_id=tenant_id,
                        agent_id=agent.id,
                        task_id=task.id,
                        anchor=anchor,
                        tool_trace_delta=checkpoint_trace_delta,
                        tokens_since_checkpoint=tokens_since_checkpoint,
                        force=True,
                        contains_restricted=contains_restricted,
                    )
                    return RunResult(
                        task.id,
                        agent.id,
                        "interrupted",
                        "Run cancelled by operator.",
                        tool_trace,
                        steps,
                        pending_runs,
                        rendered_components,
                        todos,
                        step_timings,
                    )
                # Operator chat (§ live steering): drain any messages an operator
                # sent to this running agent and inject them as user turns, so the
                # agent sees the question on its next step and answers inline
                # without derailing the task. Same READ COMMITTED cross-transaction
                # pattern as cancel_check -- a light poll of an unlocked table.
                if inbox_check is not None:
                    for operator_msg in await inbox_check():
                        messages.append(NeutralMessage(role="user", content=operator_msg))
                        await record_activity(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            status="info",
                            message=f"💬 Operator: {operator_msg}",
                        )

                ledger_hash = ledger_fingerprint(harness.state.ledger)
                if ledger_hash != harness.state.ledger_sent_hash:
                    messages.append(
                        NeutralMessage(
                            role="user",
                            content=render_ledger_block(harness.state.ledger),
                        )
                    )
                    harness.state.ledger_sent_hash = ledger_hash

                await dispatch_claude_event(
                    tenant_id,
                    "UserPromptSubmit",
                    user_prompt_submit(prompt=task_text, **_hook_ctx()),
                )

                resolved_tools = _offered()
                raw_notes = connection_config.get("tool_notes")
                tool_notes = raw_notes if isinstance(raw_notes, dict) else None
                single_routes = (
                    {tool.name: [connection_key, tool.name] for tool in tools}
                    if connection_key
                    else {}
                )
                required = [
                    req.tool
                    for skill in active_skills
                    for req in skill.definition.requires_tools
                ]
                resolved_tools, catalog = select_completion_tools(
                    resolved_tools,
                    control_names=CONTROL_TOOL_NAMES,
                    skill_names=skill_tool_names,
                    mission=task_text,
                    skill_texts=[s.definition.instruction for s in active_skills],
                    pinned=list(harness.state.pinned_tools),
                    tool_list_may_change=harness.caps.tool_list_may_change,
                    mcp_connection=connection_key,
                    tool_notes=tool_notes,
                    find_tools=FIND_TOOLS,
                    procedure_texts=_procedure_texts(active_skills),
                    connection_by_tool=connection_by_tool(single_routes),
                    allowed_connections=allowed_connections_for_skills(single_routes, required),
                )
                harness.state.tool_catalog = catalog
                if auto_cfg is not None:
                    try:
                        model_config, _route = await resolve_auto_config(
                            db,
                            auto_cfg,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            run=run_row,
                            agent=agent,
                            messages=messages,
                            tools=resolved_tools,
                            needs_vision=bool(task_images),
                            contains_restricted=contains_restricted,
                        )
                    except AutoRouterError as exc:
                        task.state = "failed"
                        await record_activity(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            status="warning",
                            message=f"{agent.name}: auto router misconfigured: {exc}",
                        )
                        return RunResult(
                            task.id,
                            agent.id,
                            "failed",
                            f"Auto model router misconfigured: {exc}",
                            tool_trace,
                            steps,
                            pending_runs,
                            rendered_components,
                            todos,
                            step_timings,
                        )
                    provider = model_config.provider
                    model = model_config.model
                    model_locality = model_config.locality
                active_route["config"] = model_config
                active_route["provider"] = provider
                active_route["model"] = model
                resolved_params = resolve_params(
                    model_config, agent=agent, definition=pinned["definition"]
                )
                # Must match what fallback.py's own base_url resolution will
                # actually send for this provider (params override, else the
                # tenant's bound credential) -- a cache key that ignores the
                # real base_url would treat two different openai_compatible
                # endpoints sharing a model name as the same request.
                resolved_base_url = (
                    (model_config.params or {}).get("base_url")
                    if model_config is not None
                    else None
                ) or await resolve_model_base_url(
                    db,
                    tenant_id=tenant_id,
                    provider=provider,
                    credential_id=model_config.credential_id if model_config is not None else None,
                )

                async def _complete(
                    sampling_params: ModelParams,
                    req_id: uuid.UUID,
                    msgs: list[NeutralMessage],
                    *,
                    tls: list[NeutralTool] = resolved_tools,
                    publish: bool = True,
                    timing: StreamTiming | None = None,
                ) -> Any:
                    return await accumulate_stream(
                        stream_completion_with_fallback(
                            db,
                            router,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            primary=active_route["config"],
                            no_config_provider=active_route["provider"],
                            no_config_model=active_route["model"],
                            messages=msgs,
                            tools=tls,
                            params=sampling_params,
                            request_id=req_id,
                            contains_restricted=contains_restricted,
                        ),
                        on_text=_live_token_delta if publish else None,
                        timing=timing,
                    )

                async def _record(res: Any, req_id: uuid.UUID) -> None:
                    await record_usage(
                        db,
                        tenant_id=tenant_id,
                        request_id=req_id,
                        model=res.model,
                        provider=res.provider,
                        tokens_in=res.usage.tokens_in,
                        tokens_out=res.usage.tokens_out,
                        agent_id=agent.id,
                        department_id=agent.department_id,
                        skill_id=active_skills[-1].skill_id if active_skills else None,
                        skill_version_id=(
                            active_skills[-1].skill_version_id if active_skills else None
                        ),
                        creator_id=active_skills[-1].creator_id if active_skills else None,
                    )

                async def _compact(sampling_params: ModelParams = resolved_params) -> None:
                    nonlocal tokens_since_checkpoint
                    summary_request_id = uuid.uuid4()
                    summary_messages = [
                        *messages,
                        NeutralMessage(role="user", content=compaction_instruction()),
                    ]
                    summary_result = await _complete(
                        sampling_params,
                        summary_request_id,
                        summary_messages,
                        publish=False,
                    )
                    await _record(summary_result, summary_request_id)
                    tokens_since_checkpoint += (
                        summary_result.usage.tokens_in + summary_result.usage.tokens_out
                    )
                    messages[:] = rebuild_transcript(
                        messages,
                        summary=summary_result.text,
                        ledger_block=render_ledger_block(harness.state.ledger),
                        skill_blocks=[
                            _instruction_for(skill, harness) for skill in active_skills
                        ],
                    )
                    harness.state.compactions += 1
                    harness.state.last_compacted_step = harness.state.step_no
                    harness.state.ledger_sent_hash = ledger_fingerprint(harness.state.ledger)

                if should_compact(harness.state, harness.caps):
                    await _compact()

                request_id = uuid.uuid4()
                resolved_messages, harness.state.masked = mask_observations(
                    messages,
                    step_no=harness.state.step_no,
                    ledger=harness.state.ledger,
                )

                step_rec = start_step(steps)
                step_timings.append(step_rec)
                step_probe = StreamTiming()

                async def _cache_lookup(
                    msgs: list[NeutralMessage],
                    *,
                    base_url: str | None = resolved_base_url,
                    tls: list[NeutralTool] = resolved_tools,
                    sampling_params: ModelParams = resolved_params,
                ) -> tuple[str | None, Any]:
                    return await cache_flow.lookup(
                        department=department,
                        tenant_id=tenant_id,
                        department_id=agent.department_id,
                        provider=active_route["provider"],
                        model=active_route["model"],
                        base_url=base_url,
                        messages=msgs,
                        tools=tls,
                        params=sampling_params,
                        contains_restricted=contains_restricted,
                    )

                key, cached_result = await _cache_lookup(resolved_messages)

                overflow_retried = False

                async def _complete_with_overflow_retry(
                    sampling_params: ModelParams,
                    req_id: uuid.UUID,
                ) -> tuple[Any, uuid.UUID]:
                    nonlocal key, resolved_messages, overflow_retried
                    stamped = with_prompt_cache_key(
                        sampling_params, str(run_id) if run_id is not None else None
                    )
                    try:
                        return (
                            await _complete(
                                stamped,
                                req_id,
                                resolved_messages,
                                timing=step_probe,
                            ),
                            req_id,
                        )
                    except Exception as exc:
                        if overflow_tokens(str(exc)) is None:
                            raise
                        if overflow_retried or already_compacted_this_step(harness.state):
                            raise
                        overflow_retried = True
                        await _compact()
                        resolved_messages, harness.state.masked = mask_observations(
                            messages,
                            step_no=harness.state.step_no,
                            ledger=harness.state.ledger,
                        )
                        key, _ = await _cache_lookup(resolved_messages)
                        retry_request_id = uuid.uuid4()
                        return (
                            await _complete(
                                stamped,
                                retry_request_id,
                                resolved_messages,
                                timing=step_probe,
                            ),
                            retry_request_id,
                        )

                if cached_result is not None:
                    result = cached_result
                    note_model(step_rec, model_wait_ms=0, ttft_ms=None)
                    await record_usage(
                        db,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        model=result.model,
                        provider=result.provider,
                        tokens_in=0,
                        tokens_out=0,
                        agent_id=agent.id,
                        department_id=agent.department_id,
                        skill_id=active_skills[-1].skill_id if active_skills else None,
                        skill_version_id=(
                            active_skills[-1].skill_version_id if active_skills else None
                        ),
                        creator_id=active_skills[-1].creator_id if active_skills else None,
                        cache_hit=True,
                        saved_tokens_in=result.usage.tokens_in,
                        saved_tokens_out=result.usage.tokens_out,
                    )
                else:

                    async def _apply_auto_escalation(
                        reason: str,
                        msgs: list[NeutralMessage] = resolved_messages,
                        tls: list[NeutralTool] = resolved_tools,
                    ) -> bool:
                        """Bump tier and refresh the concrete target. True if it changed."""
                        nonlocal model_config, provider, model, model_locality, resolved_params
                        if auto_cfg is None:
                            return False
                        bumped = await escalate_auto_router(
                            db,
                            auto_cfg,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            run=run_row,
                            agent=agent,
                            reason=reason,
                            messages=msgs,
                            tools=tls,
                            needs_vision=bool(task_images),
                            contains_restricted=contains_restricted,
                        )
                        if bumped is None:
                            return False
                        model_config, _decision = bumped
                        provider = model_config.provider
                        model = model_config.model
                        model_locality = model_config.locality
                        resolved_params = resolve_params(model_config, agent=agent)
                        active_route["config"] = model_config
                        active_route["provider"] = provider
                        active_route["model"] = model
                        return True

                    try:
                        result, request_id = await _complete_with_overflow_retry(
                            resolved_params,
                            request_id,
                        )
                    except Exception:
                        # Retry exhaustion / hard provider failure: one-way escalate
                        # and retry once on a stronger tier when Auto is configured.
                        if auto_cfg is None or not await _apply_auto_escalation(
                            "retry_exhaustion"
                        ):
                            raise
                        request_id = uuid.uuid4()
                        result, request_id = await _complete_with_overflow_retry(
                            resolved_params,
                            request_id,
                        )
                    await _record(result, request_id)
                    if not result.tool_calls:
                        result.tool_calls = _salvage_tool_calls(result.text, _offered())
                    if (
                        result.stop_reason == "length"
                        and not result.tool_calls
                        and not result.text.strip()
                    ):
                        # A reasoning-capable model can spend its whole completion
                        # budget on hidden reasoning and hit max_tokens before
                        # writing anything visible -- length-truncation with
                        # nothing produced is never a real stop. One retry with
                        # double the budget, before this silently reads as "the
                        # agent finished" with nothing actually done.
                        retry_request_id = uuid.uuid4()
                        result, retry_request_id = await _complete_with_overflow_retry(
                            bumped_for_length_retry(resolved_params),
                            retry_request_id,
                        )
                        await _record(result, retry_request_id)
                        if not result.tool_calls:
                            result.tool_calls = _salvage_tool_calls(result.text, _offered())
                    # Phase-2 cascade: heuristic or cheap self-check, then escalate once.
                    if (
                        auto_cfg is not None
                        and not cascade_flag["attempted"]
                        and cascade_verify_enabled(auto_cfg)
                        and not result.tool_calls
                        and model_config is not None
                    ):
                        cascade_flag["attempted"] = True
                        verdict = await cascade_should_escalate(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            answer=result.text,
                            messages=resolved_messages,
                            verifier_config=model_config,
                            contains_restricted=contains_restricted,
                        )
                        if verdict.escalate and await _apply_auto_escalation(
                            f"cascade_verify:{verdict.via}"
                        ):
                            cascade_flag["escalated"] = True
                            record_preference_label(
                                auto_cfg,
                                messages=resolved_messages,
                                needs_strong=True,
                                source=f"cascade:{verdict.via}",
                            )
                            cascade_request_id = uuid.uuid4()
                            result, cascade_request_id = await _complete_with_overflow_retry(
                                resolved_params,
                                cascade_request_id,
                            )
                            await _record(result, cascade_request_id)
                            if not result.tool_calls:
                                result.tool_calls = _salvage_tool_calls(result.text, _offered())
                    await cache_flow.store_if_matching(key, result, provider=provider, model=model)
                    note_model(
                        step_rec,
                        model_wait_ms=step_probe.model_wait_ms,
                        ttft_ms=step_probe.ttft_ms,
                    )
                harness.state.last_prompt_tokens = (
                    result.usage.tokens_in
                    if result.usage.tokens_in > 0
                    else prompt_token_fallback(resolved_messages)
                )

                tokens_since_checkpoint += result.usage.tokens_in + result.usage.tokens_out

                if not result.tool_calls:
                    result.tool_calls = _salvage_tool_calls(result.text, _offered())

                if not result.tool_calls:
                    await _finish_step_timing(step_rec, 0)
                    if result.stop_reason == "length" and not result.text.strip():
                        # Truncated even after the retry above -- the model
                        # never produced an answer or a tool call, so this must
                        # never read as "done" (a genuine finish always has at
                        # least the short summary system_prompt requires).
                        task.state = "failed"
                        await record_activity(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            status="warning",
                            message=(
                                f"{agent.name}: model exceeded its token budget "
                                "without producing an answer"
                            ),
                        )
                        await maybe_checkpoint(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            task_id=task.id,
                            anchor=anchor,
                            tool_trace_delta=checkpoint_trace_delta,
                            tokens_since_checkpoint=tokens_since_checkpoint,
                            force=True,
                            contains_restricted=contains_restricted,
                        )
                        await dispatch_claude_event(tenant_id, "Stop", _hook_ctx())
                        return RunResult(
                            task.id,
                            agent.id,
                            "failed",
                            "Model exceeded its token budget without producing an answer or "
                            "tool call.",
                            tool_trace,
                            steps,
                            pending_runs,
                            rendered_components,
                            todos,
                            step_timings,
                        )

                    open_todos = [t for t in todos if t.get("status") != "completed"]
                    finish_verdict = harness.may_finish(
                        open_todos, procedures=_active_procedures(active_skills)
                    )
                    if not finish_verdict.ok:
                        # D1: the model tried to finish while its own checklist
                        # still has open items. Append its (otherwise-dropped)
                        # turn plus the reminder and go around again instead of
                        # returning "done" -- bounded on its own cap, but each
                        # round still consumes one `steps` iteration.
                        messages.append(
                            NeutralMessage(role="assistant", content=result.text, tool_calls=[])
                        )
                        messages.append(
                            NeutralMessage(role="user", content=finish_verdict.reminder or "")
                        )
                        continue

                    # Reaching here with todos still open means the round cap was
                    # hit, not that everything got done -- say so in the output
                    # instead of silently looking like a clean finish.
                    output_text = result.text
                    if finish_verdict.exhausted_note is not None:
                        output_text = f"{output_text}\n\n{finish_verdict.exhausted_note}"

                    task.state = "done"
                    if auto_cfg is not None:
                        # Task-success label: escalated runs needed strong;
                        # clean finishes teach weak_ok for similar prompts.
                        ar = (
                            ((run_row.context or {}).get("auto_router") or {})
                            if run_row is not None
                            else {}
                        )
                        escalated = bool(ar.get("escalated")) or bool(
                            cascade_flag.get("escalated")
                        )
                        record_preference_label(
                            auto_cfg,
                            messages=messages,
                            needs_strong=escalated,
                            source=(
                                "task_success_escalated" if escalated else "task_success_weak"
                            ),
                        )
                        remember_agent_route(
                            agent, auto_cfg, run=run_row, concrete=model_config
                        )
                    await record_activity(
                        db,
                        tenant_id=tenant_id,
                        agent_id=agent.id,
                        status=(
                            "warning"
                            if finish_verdict.exhausted_note is not None
                            else "success"
                        ),
                        message=f"{agent.name} completed: {task_text[:80]}",
                        detail=output_text[:500] or None,
                        cache_hit=cached_result is not None,
                    )
                    await maybe_checkpoint(
                        db,
                        tenant_id=tenant_id,
                        agent_id=agent.id,
                        task_id=task.id,
                        anchor=anchor,
                        tool_trace_delta=checkpoint_trace_delta,
                        tokens_since_checkpoint=tokens_since_checkpoint,
                        force=True,
                        contains_restricted=contains_restricted,
                    )
                    await dispatch_claude_event(tenant_id, "Stop", _hook_ctx())
                    return RunResult(
                        task.id,
                        agent.id,
                        "done",
                        output_text,
                        tool_trace,
                        steps,
                        pending_runs,
                        rendered_components,
                        todos,
                        step_timings,
                    )

                messages.append(
                    NeutralMessage(
                        role="assistant", content=result.text, tool_calls=result.tool_calls
                    )
                )
                # A skill's value_threshold guardrail declares BOTH its threshold
                # and the argument key it measures (`metric`). That key is
                # use-case config owned by the skill, so merge it into the value
                # spec here rather than assuming any key in the core.
                skill_metric_keys = [
                    g.metric
                    for s in active_skills
                    for g in s.definition.guardrails
                    if g.type == "value_threshold" and g.then == "require_approval" and g.metric
                ]
                call_value_spec: dict[str, Any] | None = value_spec
                if skill_metric_keys:
                    merged = dict(value_spec or {})
                    merged["direct_fields"] = [
                        *(merged.get("direct_fields") or []),
                        *skill_metric_keys,
                    ]
                    call_value_spec = merged
                step_had_tool_error = False
                step_tool_wait_ms = 0
                # Spec §3.5: a turn's LEADING run of ALLOW-decision, read-tier,
                # non-control, non-outward tool calls dispatches concurrently
                # (bounded) when this connection's caps say the model can
                # cope with that. Everything from the first call that breaks
                # the run onward (a write, a control tool, a non-ALLOW
                # decision, an outward-declared call) still goes through the
                # per-call loop below unchanged. Batch eligibility uses B0,
                # tier, and outward classification; the read-tier gate runs
                # later in the normal per-call ordering after hooks.
                precomputed_outputs: dict[
                    str, tuple[str, dt.datetime, int, ToolError | None]
                ] = {}
                call_justifications: dict[str, str] = {}
                if (
                    harness.caps.parallel_tool_calls
                    and server is not None
                    and not isinstance(server, RoutedToolset)
                ):
                    read_batch: list[ToolCall] = []
                    for _pre_tc in result.tool_calls:
                        if _pre_tc.name in CONTROL_TOOL_NAMES:
                            break
                        pre_decision = _authorize(
                            agent,
                            _pre_tc,
                            frame=frame,
                            delegation_depth=task.delegation_depth,
                            tool_policies=tool_policies,
                            connection_key=connection_key,
                            tool_scopes=tool_scopes,
                            skill_tool_names=skill_tool_names,
                            value_spec=call_value_spec,
                            guardrail_attribute_specs=guardrail_attribute_specs,
                            skill_thresholds=tuple(
                                g.gt
                                for s in active_skills
                                for g in s.definition.guardrails
                                if g.type == "value_threshold" and g.then == "require_approval"
                            ),
                        )
                        stripped, justification = strip_justification(_pre_tc.arguments)
                        _pre_tc.arguments = stripped
                        call_justifications[_pre_tc.id] = justification
                        if pre_decision.effect is Effect.REQUIRE_APPROVAL and pre_decided:
                            verdict = pre_decided.get(_call_sig(_pre_tc))
                            if verdict == "approve":
                                pre_decision = Decision(Effect.ALLOW, "operator approved")
                        if pre_decision.effect is not Effect.ALLOW:
                            break
                        if classify_tier(
                            _pre_tc.name,
                            scopes=tool_scopes,
                            config=connection_config,
                            annotations=next(
                                (
                                    tool.annotations
                                    for tool in _offered()
                                    if tool.name == _pre_tc.name
                                ),
                                None,
                            ),
                            arguments=_pre_tc.arguments,
                        ) != "read":
                            break
                        if (
                            outward_target(
                                _pre_tc.name,
                                _pre_tc.arguments,
                                focus_spec,
                                outward_tools,
                                skip_spec=(
                                    connection_config.get("outward_skip_spec")
                                    if isinstance(connection_config, dict)
                                    else None
                                ),
                            )
                            is not None
                        ):
                            break
                        read_batch.append(_pre_tc)
                    if len(read_batch) > 1:
                        _read_batch_semaphore = asyncio.Semaphore(5)

                        async def _dispatch_precomputed(
                            call: ToolCall,
                            _sem: asyncio.Semaphore = _read_batch_semaphore,
                        ) -> tuple[str, str, dt.datetime, int, ToolError | None]:
                            async with _sem:
                                started_at = dt.datetime.now(dt.UTC)
                                tool_error: ToolError | None = None
                                try:
                                    result_text = await server.call(call.name, call.arguments)
                                except Exception as exc:  # surface tool errors to the model
                                    tool_error = classify_exception(
                                        exc,
                                        duration_s=(
                                            dt.datetime.now(dt.UTC) - started_at
                                        ).total_seconds(),
                                    )
                                    result_text = f"ERROR: {exc}"
                                duration_ms = int(
                                    (dt.datetime.now(dt.UTC) - started_at).total_seconds() * 1000
                                )
                                return call.id, result_text, started_at, duration_ms, tool_error

                        for (
                            call_id,
                            result_text,
                            started_at,
                            duration_ms,
                            precomputed_error,
                        ) in await asyncio.gather(
                            *(_dispatch_precomputed(call) for call in read_batch)
                        ):
                            precomputed_outputs[call_id] = (
                                result_text,
                                started_at,
                                duration_ms,
                                precomputed_error,
                            )
                for tc in result.tool_calls:
                    call_key = connection_key
                    call_scopes = tool_scopes
                    call_guardrails = guardrail_attribute_specs
                    per_value = value_spec
                    call_focus = focus_spec
                    call_outward = outward_tools
                    auth_tc = tc
                    if isinstance(server, RoutedToolset):
                        found = server.route(tc.name)
                        if found is not None:
                            bundle = server.auth_by_connection.get(found.connection)
                            if isinstance(bundle, _McpAuth):
                                call_key = bundle.connection_key
                                call_scopes = bundle.tool_scopes
                                call_guardrails = bundle.guardrail_attribute_specs
                                per_value = bundle.value_spec
                                call_focus = bundle.focus_spec
                                call_outward = bundle.outward_tools
                            auth_tc = ToolCall(id=tc.id, name=found.tool, arguments=tc.arguments)
                    owner = next((item for item in extra_conns if item.name == call_key), None)
                    call_config = (
                        owner.config
                        if owner is not None and isinstance(owner.config, dict)
                        else connection_config
                    )
                    call_value_spec: dict[str, Any] | None = per_value
                    if skill_metric_keys:
                        merged = dict(per_value or {})
                        merged["direct_fields"] = [
                            *(merged.get("direct_fields") or []),
                            *skill_metric_keys,
                        ]
                        call_value_spec = merged
                    decision = _authorize(
                        agent,
                        auth_tc,
                        frame=frame,
                        delegation_depth=task.delegation_depth,
                        tool_policies=tool_policies,
                        connection_key=call_key,
                        tool_scopes=call_scopes,
                        skill_tool_names=skill_tool_names,
                        value_spec=call_value_spec,
                        guardrail_attribute_specs=call_guardrails,
                        narrowing=pinned["narrowing"] or {},
                        is_team_lead=bool(pinned["is_team_lead"]),
                        skill_thresholds=tuple(
                            g.gt
                            for s in active_skills
                            for g in s.definition.guardrails
                            if g.type == "value_threshold" and g.then == "require_approval"
                        ),
                    )
                    if tc.id in call_justifications:
                        justification = call_justifications[tc.id]
                    else:
                        stripped, justification = strip_justification(tc.arguments)
                        tc.arguments = stripped
                    # Resume of a previously-suspended run: an operator already
                    # decided this exact call. Honour that instead of suspending
                    # again -- approve executes it, reject turns it into a DENY
                    # the model sees and moves past.
                    if decision.effect is Effect.REQUIRE_APPROVAL and pre_decided:
                        verdict = pre_decided.get(_call_sig(tc))
                        if verdict == "approve":
                            decision = Decision(Effect.ALLOW, "operator approved")
                        elif verdict == "reject":
                            decision = Decision(Effect.DENY, "operator rejected this action")
                    record_tool_call(tool=tc.name, decision=decision.effect.value)
                    with get_tracer().start_as_current_span("tool.call") as tool_span:
                        tool_span.set_attribute("tool", tc.name)
                        tool_span.set_attribute("decision", decision.effect.value)
                        await append_event(
                            db,
                            tenant_id=tenant_id,
                            actor_type="agent",
                            actor_id=agent.id,
                            category="tool_action",
                            action=f"tool.call:{tc.name}",
                            resource={"tool": tc.name, "arguments": tc.arguments},
                            decision=decision.effect.value,
                            reason=decision.reason or None,
                            originating_operator=originating_operator,
                        )
                        pre_hook = await dispatch_claude_event(
                            tenant_id,
                            "PreToolUse",
                            tool_event(
                                tool_name=tc.name,
                                tool_input=tc.arguments,
                                **_hook_ctx(),
                            ),
                            tool_name=tc.name,
                        )
                        if pre_hook.blocked:
                            output = f"ERROR: {pre_hook.reason or 'blocked by plugin hook'}"
                            tool_trace.append(
                                {
                                    "tool": tc.name,
                                    "arguments": tc.arguments,
                                    "result": output[:300],
                                    "step": steps,
                                    "connection": connection_key,
                                    # A plugin hook refusing the call is a
                                    # denial, not a failure: nothing was
                                    # attempted, so the timeline must not
                                    # render it as the tool breaking.
                                    "state": "denied",
                                }
                            )
                            await _live_tool_call(tool_trace[-1])
                            messages.append(
                                NeutralMessage(
                                    role="tool",
                                    content=output,
                                    tool_call_id=tc.id,
                                    name=tc.name,
                                )
                            )
                            harness.state.repeat, repeat_reminder = track_repeat_tool_call(
                                harness.state.repeat, tc
                            )
                            if repeat_reminder is not None:
                                messages.append(
                                    NeutralMessage(role="user", content=repeat_reminder)
                                )
                            checkpoint_trace_delta.append(tool_trace[-1])
                            if auto_cfg is not None:
                                bumped = await escalate_auto_router(
                                    db,
                                    auto_cfg,
                                    tenant_id=tenant_id,
                                    agent_id=agent.id,
                                    run=run_row,
                                    agent=agent,
                                    reason="tool_error",
                                    messages=messages,
                                    tools=_offered(),
                                    needs_vision=bool(task_images),
                                    contains_restricted=contains_restricted,
                                )
                                if bumped is not None:
                                    model_config, _decision = bumped
                                    provider = model_config.provider
                                    model = model_config.model
                                    model_locality = model_config.locality
                            await dispatch_claude_event(
                                tenant_id,
                                "PostToolUseFailure",
                                tool_result(
                                    tool_name=tc.name,
                                    tool_input=tc.arguments,
                                    result=output,
                                    **_hook_ctx(),
                                ),
                                tool_name=tc.name,
                            )
                            continue
                        gate_verdict = None
                        if decision.effect is Effect.ALLOW:
                            gate_verdict = _gate(
                                auth_tc,
                                scopes=call_scopes,
                                config=call_config,
                                connection=call_key,
                                spec=call_focus,
                            )
                            if gate_verdict.effect == "ask":
                                verdict = pre_decided.get(_call_sig(tc)) if pre_decided else None
                                if verdict == "approve":
                                    decision = Decision(Effect.ALLOW, "operator approved")
                                elif verdict == "reject":
                                    decision = Decision(
                                        Effect.DENY, "operator rejected this action"
                                    )
                                else:
                                    decision = Decision(
                                        Effect.REQUIRE_APPROVAL, gate_verdict.preview
                                    )
                            elif gate_verdict.effect == "deny":
                                decision = Decision(Effect.DENY, gate_verdict.reason)
                            tool_span.set_attribute("decision", decision.effect.value)
                        if decision.effect is Effect.REQUIRE_APPROVAL:
                            task.state = "waiting_for_approval"
                            agent.status = "waiting_for_approval"
                            await publish_agent_status(agent)
                            if tc.name == "memory_write":
                                record = await write_memory(
                                    db,
                                    tenant_id=tenant_id,
                                    agent=agent,
                                    tier=str(tc.arguments.get("tier", "")),
                                    content=str(tc.arguments.get("content", "")),
                                    metadata={"task_id": str(task.id)},
                                )
                                ar = await raise_approval(
                                    db,
                                    tenant_id=tenant_id,
                                    agent_id=agent.id,
                                    task_id=task.id,
                                    action_type="memory_write",
                                    title=f"{agent.name} wants to write company memory",
                                    detail=decision.reason,
                                    payload={
                                        "memory_record_id": str(record.id),
                                        "tier": str(tc.arguments.get("tier", "")),
                                        "content": str(tc.arguments.get("content", "")),
                                        "justification": justification,
                                        "preview": (
                                            gate_verdict.preview
                                            if gate_verdict is not None
                                            else ""
                                        ),
                                    },
                                    reason_code=decision.reason_code,
                                    reason_context=decision.context,
                                )
                            else:
                                link = await _record_url(
                                    auth_tc.name, tc.arguments, call_key, call_focus
                                )
                                ar = await raise_approval(
                                    db,
                                    tenant_id=tenant_id,
                                    agent_id=agent.id,
                                    task_id=task.id,
                                    action_type="tool_send",
                                    title=f"{agent.name} wants to call {tc.name}",
                                    detail=decision.reason,
                                    payload={
                                        "tool": tc.name,
                                        "arguments": tc.arguments,
                                        "justification": justification,
                                        "preview": (
                                            gate_verdict.preview
                                            if gate_verdict is not None
                                            else ""
                                        ),
                                        **({"record_url": link} if link else {}),
                                    },
                                    reason_code=decision.reason_code,
                                    reason_context=decision.context,
                                )

                            from oc8.realtime.bus import get_event_bus

                            await get_event_bus().publish_event(
                                ar.tenant_id,
                                "approval.created",
                                {
                                    "approval_id": str(ar.id),
                                    "action_type": ar.action_type,
                                    "status": ar.status,
                                    # Carried in the envelope on purpose: the
                                    # executor commits, not us, so a fresh
                                    # session reading this row would see
                                    # nothing (see EventBus._push_payload).
                                    "title": ar.title,
                                    "detail": ar.detail,
                                },
                                source=f"oc8/approval/{ar.id}",
                            )
                            tool_trace.append(
                                {
                                    "tool": tc.name,
                                    "arguments": tc.arguments,
                                    # `decision` stays: approval_resume.py's
                                    # sibling list uses the same word, and
                                    # removing a key nothing forced us to
                                    # remove is how an old run's record stops
                                    # rendering. `state` is the field the
                                    # timeline reads.
                                    "decision": "require_approval",
                                    "step": steps,
                                    "connection": connection_key,
                                    "state": "awaiting_approval",
                                    "reason": decision.reason,
                                }
                            )
                            await _live_tool_call(tool_trace[-1])
                            checkpoint_trace_delta.append(tool_trace[-1])
                            await maybe_checkpoint(
                                db,
                                tenant_id=tenant_id,
                                agent_id=agent.id,
                                task_id=task.id,
                                anchor=anchor,
                                tool_trace_delta=checkpoint_trace_delta,
                                tokens_since_checkpoint=tokens_since_checkpoint,
                                force=True,
                                contains_restricted=contains_restricted,
                            )
                            await _finish_step_timing(step_rec, step_tool_wait_ms)
                            return RunResult(
                                task.id,
                                agent.id,
                                "waiting_for_approval",
                                decision.reason,
                                tool_trace,
                                steps,
                                pending_runs,
                                rendered_components,
                                todos,
                                step_timings,
                            )

                        # Ledger outward attribution must follow the tool that
                        # actually ran. B4 may replace tc; keep the gated name.
                        gated_tool_name = tc.name
                        if (
                            decision.effect is Effect.ALLOW
                            and gate_verdict is not None
                            and should_clarify(
                                tier=gate_verdict.tier,
                                autonomy=autonomy,
                                granted=gate_verdict.tier in b5_grants,
                                clarify_enabled=definition.get("clarify_before_irreversible")
                                is not False,
                                already_done=harness.state.clarification_done,
                            )
                        ):
                            clarify_tier = gate_verdict.tier
                            clarify_tool = tc.name
                            run_context = next(
                                (
                                    msg.content
                                    for msg in messages
                                    if msg.role == "user"
                                    and isinstance(msg.content, str)
                                    and msg.content.startswith("# Run context")
                                ),
                                "",
                            )
                            call_json = json.dumps(
                                {
                                    "name": tc.name,
                                    "arguments": strip_justification(tc.arguments)[0],
                                },
                                sort_keys=True,
                            )
                            clarify_msgs = clarification_prompt(
                                task_text=task_text,
                                run_context=run_context,
                                ledger_block=render_ledger_block(harness.state.ledger),
                                call_json=call_json,
                            )
                            reply_text = "NONE"
                            try:
                                clarify_req_id = uuid.uuid4()
                                clarify_result = await _complete(
                                    replace(resolved_params, temperature=0.0),
                                    clarify_req_id,
                                    clarify_msgs,
                                    tls=[],
                                    publish=False,
                                )
                                await _record(clarify_result, clarify_req_id)
                                reply_text = clarify_result.text or ""
                            except Exception:
                                logger.warning(
                                    "clarification checkpoint failed for tool=%s",
                                    clarify_tool,
                                    exc_info=True,
                                )
                            chat = run_row is not None and run_row.source == "chat"
                            tc = apply_clarification(
                                tc, reply_text, chat=chat, state=harness.state
                            )
                            facts = parse_clarification(reply_text)
                            logger.info(
                                "clarification checkpoint tool=%s tier=%s %s",
                                clarify_tool,
                                clarify_tier,
                                facts if facts is not None else "NONE",
                            )

                        _precomputed_duration_ms: int | None = None
                        if tc.id in precomputed_outputs:
                            _, _tool_call_started_at, _, _ = precomputed_outputs[tc.id]
                        else:
                            _tool_call_started_at = dt.datetime.now(dt.UTC)
                        tool_error: ToolError | None = None
                        source = mcp_conn.name if mcp_conn is not None else "oc8"
                        # Whether this call is actually dispatched anywhere -- a
                        # control tool, or the tool server. The two branches
                        # below that refuse it before dispatch set this False so
                        # the append site omits the timing keys entirely, the
                        # same as the `pre_hook.blocked` short-circuit above:
                        # `avgToolCallDurationMs` averages calls that RAN, and a
                        # near-zero duration for one that never left the process
                        # would silently drag every denial into that average.
                        _tool_call_dispatched = True
                        writes = required_right(auth_tc.name, call_scopes) != "read"
                        offered_tool = next(
                            (tool for tool in _offered() if tool.name == tc.name), None
                        )
                        idempotent = (
                            offered_tool is not None
                            and offered_tool.annotations is not None
                            and offered_tool.annotations.get("idempotentHint") is True
                        )
                        access_identity = record_identity(auth_tc.name, tc.arguments, call_focus)
                        identity = access_identity if writes else None
                        record_label = (
                            describe_focus(auth_tc.name, tc.arguments, call_focus)
                            or (
                                f"{access_identity[0]} {access_identity[1]}"
                                if access_identity is not None
                                else ""
                            )
                        )
                        control = await execute_control_tool(
                            db,
                            tenant_id=tenant_id,
                            agent=agent,
                            task=task,
                            tc=tc,
                            decision=decision,
                            assigned_skills=assigned_skills,
                            active_skills=active_skills,
                            mcp_conn=mcp_conn,
                            originating_operator=originating_operator,
                            run_id=run_id,
                            pinned=pinned,
                            harness_state=harness.state,
                            active_procedure_skills=[
                                s for s in active_skills if s.definition.steps
                            ],
                        )
                        if control is not None:
                            # A core-owned tool (memory/ask/delegate/skill). The
                            # dispatcher is shared with the isolated runtime and
                            # deliberately mutates nothing here -- it reports, we
                            # store, because the two runtimes keep this state in
                            # different places. See oc8.agent.control_tools.
                            output = control.output
                            source = "oc8"
                            if control.output.startswith("ERROR:"):
                                tool_error = ToolError(
                                    kind="control",
                                    message=control.output.removeprefix("ERROR: ").strip(),
                                )
                            if control.pending_run is not None:
                                pending_runs.append(control.pending_run)
                            if control.activated_skill is not None:
                                # Only the active-set is tracked here (it drives tool
                                # narrowing). The skill's procedure travels in the
                                # tool result itself -- no extra message is injected,
                                # see execute_control_tool.
                                active_skills.append(control.activated_skill)
                            if control.rendered_component is not None and run_id is not None:
                                from oc8.realtime.bus import get_event_bus

                                rendered_components.append(control.rendered_component)
                                await get_event_bus().publish_event(
                                    tenant_id,
                                    "run.component_rendered",
                                    {"run_id": str(run_id), **control.rendered_component},
                                    source=f"oc8/run/{run_id}",
                                )
                            if control.todos is not None:
                                # Slice-assign (not `todos = control.todos`): this
                                # closure only ever mutates the outer `todos` list
                                # in place, the same convention as pending_runs/
                                # rendered_components above -- a rebind here would
                                # need `nonlocal` and every RunResult below already
                                # closes over the one list object.
                                todos[:] = control.todos
                                if run_id is not None:
                                    # Same reasoning as run.component_rendered above:
                                    # an already-open Live Log tab's `useRun` cache
                                    # is otherwise stuck with whatever todos existed
                                    # at its initial GET fetch, since nothing else
                                    # refetches or polls it.
                                    from oc8.realtime.bus import get_event_bus

                                    await get_event_bus().publish_event(
                                        tenant_id,
                                        "run.todos_updated",
                                        {"run_id": str(run_id), "todos": control.todos},
                                        source=f"oc8/run/{run_id}",
                                    )
                            if (
                                tool_error is None
                                and not output.startswith("ERROR:")
                                and tc.name in {"request_decision", "ask_user"}
                            ):
                                record_decision(
                                    harness.state.ledger,
                                    tool=tc.name,
                                    question=str(tc.arguments.get("question", "")),
                                    step=harness.state.step_no,
                                )
                            if control.suspend == "waiting_for_input":
                                task.state = "waiting_for_input"
                                step_tool_wait_ms += int(
                                    (dt.datetime.now(dt.UTC) - _tool_call_started_at)
                                    .total_seconds()
                                    * 1000
                                )
                                await _finish_step_timing(step_rec, step_tool_wait_ms)
                                return RunResult(
                                    task.id,
                                    agent.id,
                                    "waiting_for_input",
                                    control.output,
                                    tool_trace,
                                    steps,
                                    pending_runs,
                                    rendered_components,
                                    todos,
                                    step_timings,
                                )
                        elif decision.effect is Effect.DENY or server is None:
                            reason = decision.reason or "no tool server available"
                            output = f"ERROR: {reason}"
                            source = "oc8"
                            tool_error = ToolError(kind="deny", message=reason)
                            _tool_call_dispatched = False
                        elif (
                            blast_refusal := await check_blast_radius(
                                db,
                                tenant_id=tenant_id,
                                run_id=run_id,
                                frame=frame,
                                identity=identity,
                            )
                        ) is not None:
                            output = blast_refusal
                            source = "oc8"
                            tool_error = ToolError(
                                kind="deny",
                                message=blast_refusal.removeprefix("ERROR: ").strip(),
                            )
                            _tool_call_dispatched = False
                        elif (
                            claim_refusal := await claim_write(
                                db,
                                tenant_id=tenant_id,
                                run_id=run_id,
                                agent_id=agent.id,
                                identity=identity,
                                label=record_label,
                            )
                        ) is not None:
                            output = claim_refusal
                            source = "oc8"
                            tool_error = ToolError(
                                kind="deny",
                                message=claim_refusal.removeprefix("ERROR: ").strip(),
                            )
                            _tool_call_dispatched = False
                        elif (
                            outward := await check_outward(
                                db,
                                tenant_id=tenant_id,
                                task_id=task.id,
                                tc=auth_tc,
                                focus_spec=call_focus,
                                outward_tools=call_outward,
                                skip_spec=(
                                    call_config.get("outward_skip_spec")
                                    if isinstance(call_config, dict)
                                    else None
                                ),
                            )
                        ).refusal is not None:
                            # Before the call, not after: the point is that the
                            # recipient is not reached twice, and a check that ran
                            # afterwards could only report it.
                            output = outward.refusal
                            source = "oc8"
                            tool_error = ToolError(
                                kind="deny",
                                message=outward.refusal.removeprefix("ERROR: ").strip(),
                            )
                            _tool_call_dispatched = False
                        else:
                            # Live-log which record the agent is working on, from
                            # the connection's own focus_spec (a plugin supplies
                            # it; the core names nothing software-specific).
                            focus = describe_focus(auth_tc.name, tc.arguments, call_focus)
                            if focus is not None:
                                await note_focus(
                                    db,
                                    tenant_id=tenant_id,
                                    agent_id=agent.id,
                                    task_id=task.id,
                                    focus=focus,
                                    specific=describes_a_record(
                                        auth_tc.name, tc.arguments, call_focus
                                    ),
                                    cache_hit=cached_result is not None,
                                    record_url=await _record_url(
                                        auth_tc.name, tc.arguments, call_key, call_focus
                                    ),
                                )
                            replay = await replay_for(
                                db,
                                tenant_id=tenant_id,
                                task_id=task.id,
                                tc=tc,
                                writes=writes,
                                idempotent=idempotent,
                            )
                            if replay is not None:
                                output = replay
                            else:
                                if tc.id in precomputed_outputs:
                                    (
                                        output,
                                        _,
                                        _precomputed_duration_ms,
                                        tool_error,
                                    ) = precomputed_outputs.pop(tc.id)
                                else:
                                    try:
                                        output = await server.call(tc.name, tc.arguments)
                                    except ElicitationNeeded as exc:
                                        if run_id is not None:
                                            parked = await db.get(m.AgentRun, run_id)
                                            if parked is not None:
                                                parked_ctx = dict(parked.context or {})
                                                parked_ctx["pending_elicitation"] = {
                                                    "connection": connection_key or "",
                                                    "tool": tc.name,
                                                    "arguments": dict(tc.arguments),
                                                    "question": exc.message,
                                                }
                                                parked.context = parked_ctx
                                        task.state = "waiting_for_input"
                                        await _finish_step_timing(step_rec, step_tool_wait_ms)
                                        return RunResult(
                                            task.id,
                                            agent.id,
                                            "waiting_for_input",
                                            exc.message,
                                            tool_trace,
                                            steps,
                                            pending_runs,
                                            rendered_components,
                                            todos,
                                            step_timings,
                                        )
                                    except Exception as exc:  # surface tool errors to the model
                                        tool_error = classify_exception(
                                            exc,
                                            duration_s=(
                                                dt.datetime.now(dt.UTC) - _tool_call_started_at
                                            ).total_seconds(),
                                        )
                                        output = f"ERROR: {exc}"
                                await remember_outward(
                                    db,
                                    tenant_id=tenant_id,
                                    task_id=task.id,
                                    target=outward.target,
                                    output=output,
                                )
                                await record_for(
                                    db,
                                    tenant_id=tenant_id,
                                    task_id=task.id,
                                    tc=tc,
                                    writes=writes,
                                    output=output,
                                    idempotent=idempotent,
                                )
                        succeeded = tool_error is None and not output.startswith("ERROR:")
                        # harness.shape() below prepends a "[step N/max · ...]"
                        # stamp to EVERY shaped result, so the reassigned
                        # `output` no longer starts with a literal "ERROR:"
                        # even for a genuine dispatched failure. call_state_for
                        # needs the pre-stamp text to tell "failed" from
                        # "done" -- `succeeded` just above is computed from
                        # this same unshaped value.
                        _tool_call_output_for_state = output
                        procs = _active_procedures(active_skills)
                        before_sat = _satisfied_map(procs, harness)
                        if succeeded:
                            record_tool(harness.state.ledger, tc.name)
                            if access_identity is not None and mcp_conn is not None:
                                note_access(
                                    harness.state.ledger,
                                    connection=mcp_conn.name,
                                    kind=access_identity[0],
                                    id=access_identity[1],
                                    label=record_label,
                                    step_no=harness.state.step_no,
                                    wrote=writes,
                                    tool=tc.name,
                                    exempt_unverified=(
                                        gate_verdict is not None
                                        and gate_verdict.tier == "outward"
                                        and tc.name == gated_tool_name
                                    ),
                                )
                            if (
                                gate_verdict is not None
                                and gate_verdict.tier == "outward"
                                and tc.name == gated_tool_name
                            ):
                                record_outward(
                                    harness.state.ledger,
                                    connection=connection_key or "oc8",
                                    tool=tc.name,
                                    target=str(
                                        tc.arguments.get("target")
                                        or tc.arguments.get("to")
                                        or ""
                                    ),
                                    step=harness.state.step_no,
                                )
                            if tc.name == "write_output_file":
                                record_file(
                                    harness.state.ledger,
                                    str(tc.arguments.get("filename", "")),
                                )
                        after_sat = _satisfied_map(procs, harness)
                        flip_lines = newly_satisfied_lines(
                            skills=procs, before=before_sat, after=after_sat
                        )
                        shaped = harness.shape(
                            tc,
                            output,
                            max_steps=max_steps,
                            tz=tz,
                            source=source,
                            error=tool_error,
                        )
                        output = shaped.output
                        shaped.reminders.extend(flip_lines)
                        if succeeded and shaped.spill is not None:
                            record_file(harness.state.ledger, shaped.spill.filename)
                        if shaped.spill is not None and run_id is not None:
                            try:
                                await persist_spill(
                                    db,
                                    tenant_id=tenant_id,
                                    run_id=run_id,
                                    spill=shaped.spill,
                                )
                            except Exception:
                                logger.exception(
                                    "run %s: spill persistence escaped safe boundary", run_id
                                )
                        if auto_cfg is not None and (
                            output.startswith("ERROR:") or not (output or "").strip()
                        ):
                            # One-way escalate for the rest of the run; next
                            # resolve_auto_config honours the latch.
                            bumped = await escalate_auto_router(
                                db,
                                auto_cfg,
                                tenant_id=tenant_id,
                                agent_id=agent.id,
                                run=run_row,
                                agent=agent,
                                reason=(
                                    "tool_error"
                                    if output.startswith("ERROR:")
                                    else "empty_tool_result"
                                ),
                                messages=messages,
                                tools=_offered(),
                                needs_vision=bool(task_images),
                                contains_restricted=contains_restricted,
                            )
                            if bumped is not None:
                                model_config, _decision = bumped
                                provider = model_config.provider
                                model = model_config.model
                                model_locality = model_config.locality
                        _tool_call_entry: dict[str, Any] = {
                            "tool": tc.name,
                            "arguments": tc.arguments,
                            "result": output[:300],
                            "step": steps,
                            "connection": connection_key,
                            "state": call_state_for(
                                _tool_call_output_for_state, dispatched=_tool_call_dispatched
                            ),
                        }
                        if _tool_call_dispatched:
                            _tool_call_entry["startedAt"] = _tool_call_started_at.isoformat()
                            _tool_call_entry["durationMs"] = (
                                _precomputed_duration_ms
                                if _precomputed_duration_ms is not None
                                else int(
                                    (dt.datetime.now(dt.UTC) - _tool_call_started_at)
                                    .total_seconds()
                                    * 1000
                                )
                            )
                            step_tool_wait_ms += int(_tool_call_entry["durationMs"])
                        if not _tool_call_dispatched:
                            _tool_call_entry["reason"] = (
                                tool_error.message if tool_error is not None else None
                            )
                        tool_trace.append(_tool_call_entry)
                        await _live_tool_call(tool_trace[-1])
                        messages.append(
                            NeutralMessage(
                                role="tool", content=output, tool_call_id=tc.id, name=tc.name
                            )
                        )
                        for reminder in shaped.reminders:
                            messages.append(NeutralMessage(role="user", content=reminder))
                        checkpoint_trace_delta.append(tool_trace[-1])
                        step_had_tool_error = step_had_tool_error or tool_error is not None
                        post_event = (
                            "PostToolUseFailure" if tool_error is not None else "PostToolUse"
                        )
                        await dispatch_claude_event(
                            tenant_id,
                            post_event,
                            tool_result(
                                tool_name=tc.name,
                                tool_input=tc.arguments,
                                result=output,
                                **_hook_ctx(),
                            ),
                            tool_name=tc.name,
                        )
                        checkpoint = await maybe_checkpoint(
                            db,
                            tenant_id=tenant_id,
                            agent_id=agent.id,
                            task_id=task.id,
                            anchor=anchor,
                            tool_trace_delta=checkpoint_trace_delta,
                            tokens_since_checkpoint=tokens_since_checkpoint,
                            force=False,
                            contains_restricted=contains_restricted,
                        )
                        if checkpoint is not None:
                            checkpoint_trace_delta = []
                            tokens_since_checkpoint = 0

                if cached_result is None and step_had_tool_error:
                    await cache_flow.invalidate(key)

                await _finish_step_timing(step_rec, step_tool_wait_ms)

            task.state = "done"
            await maybe_checkpoint(
                db,
                tenant_id=tenant_id,
                agent_id=agent.id,
                task_id=task.id,
                anchor=anchor,
                tool_trace_delta=checkpoint_trace_delta,
                tokens_since_checkpoint=tokens_since_checkpoint,
                force=True,
                contains_restricted=contains_restricted,
            )
            await dispatch_claude_event(tenant_id, "Stop", _hook_ctx())
            return RunResult(
                task.id,
                agent.id,
                "done",
                "Reached step limit.",
                tool_trace,
                steps,
                pending_runs,
                rendered_components,
                todos,
                step_timings,
            )

        async def _run_with_session_end() -> RunResult:
            try:
                if toolset is not None:
                    return await loop(toolset.tools, toolset)
                def _note_unavailable(conn: m.McpConnection, exc: BaseException) -> None:
                    reason = " ".join(str(exc).split())[:200] or type(exc).__name__
                    startup_unavailable.append({"name": conn.name, "reason": reason})
                    messages.append(
                        NeutralMessage(
                            role="user",
                            content=unavailable_sentence(conn.name, reason),
                        )
                    )
                    logger.warning(
                        "connection %s (%s) offers no tools right now; the rest stay available",
                        conn.name,
                        conn.id,
                        exc_info=True,
                    )

                if not extra_conns:
                    return await loop([], None)
                if len(extra_conns) == 1:
                    only = extra_conns[0]
                    cfg = only.config if isinstance(only.config, dict) else {}
                    try:
                        tool_session = await _open_mcp_session(
                            db, tenant_id=tenant_id, mcp_conn=only
                        )
                        async with tool_session as server:
                            return await loop(apply_tool_notes(server.tools, cfg), server)
                    except Exception as exc:
                        _note_unavailable(only, exc)
                        return await loop([], None)
                async with AsyncExitStack() as stack:
                    sessions: dict[str, Any] = {}
                    tools_by_connection: dict[str, list[NeutralTool]] = {}
                    auth_by_connection: dict[str, _McpAuth] = {}
                    for conn in extra_conns:
                        cfg = conn.config if isinstance(conn.config, dict) else {}
                        try:
                            opened = await _open_mcp_session(db, tenant_id=tenant_id, mcp_conn=conn)
                            session = await stack.enter_async_context(opened)
                        except Exception as exc:
                            _note_unavailable(conn, exc)
                            continue
                        sessions[conn.name] = session
                        tools_by_connection[conn.name] = apply_tool_notes(session.tools, cfg)
                        auth_by_connection[conn.name] = _mcp_auth(conn)
                    if not sessions:
                        return await loop([], None)
                    routed = RoutedToolset(
                        sessions,
                        tools_by_connection,
                        auth_by_connection=auth_by_connection,
                    )
                    return await loop(routed.tools, routed)
            finally:
                if session_state["started"]:
                    await dispatch_claude_event(tenant_id, "SessionEnd", _hook_ctx())

        return await _run_with_session_end()

    with get_tracer().start_as_current_span("agent.run") as span:
        span.set_attribute("tenant_id", str(tenant_id))
        span.set_attribute("agent_id", str(agent.id))
        result = await _run()
        span.set_attribute("status", result.status)
        return result
