"""Agent execution loop (tech-spec §8.3).

trigger -> assemble context -> LLM complete (via Model Router) -> for each tool
call: PEP authorize -> invoke MCP tool -> feed result back -> repeat until the
model stops. Every tool call is audited; token usage is metered; a threshold
breach raises a HITL approval and suspends the run.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent import cache_flow
from oc8.agent.control_tools import (
    DEPTH_LIMIT_REASON,
    MAX_DELEGATION_DEPTH,
    execute_control_tool,
    offered_tools,
)
from oc8.agent.harness.calls import (
    call_sig as _call_sig,  # re-exported for mcp_gateway.py and older tests
)
from oc8.agent.harness.stages.c_reminders import (
    TOOL_OUTPUT_BUDGET_WARNING_CHARS,
    cap_tool_output,
    tool_output_budget_reminder,
    track_repeat_tool_call,
)
from oc8.agent.harness.stages.d_todo import (
    TODO_CONTINUATION_MAX_ROUNDS,
    todo_continuation_exhausted_note,
    todo_continuation_reminder,
)
from oc8.agent.mcp_client import McpSession
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.agent.mcp_requirements import wrap_with_requirements
from oc8.agent.outward import (
    REFUSAL,
    already_delivered,
    outward_target,
    remember_delivery,
)
from oc8.agent.preamble import build_run_preamble
from oc8.agent.tool_notes import apply_tool_notes
from oc8.agent.tool_semantics import (
    describe_focus,
    describes_a_record,
    extract_attributes,
    extract_value,
)
from oc8.approvals import raise_approval
from oc8.audit import append_event
from oc8.authz.pdp import (
    Decision,
    Effect,
    ToolPolicy,
    authorize_tool_call,
    effective_tool_policies,
    required_right,
)
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
from oc8.memory.policy import authorize_memory_write
from oc8.memory.router import MAX_MEMORY_CONTENT_LENGTH, write_memory
from oc8.metering import check_budget, record_usage, trigger_budget_hard_stop
from oc8.modelrouter import (
    NeutralMessage,
    NeutralTool,
    ToolCall,
    get_model_router,
    locality_for_provider,
    stream_completion_with_fallback,
)
from oc8.modelrouter.accumulate import accumulate_stream
from oc8.modelrouter.keys import resolve_model_base_url
from oc8.modelrouter.sampling import bumped_for_length_retry, resolve_params
from oc8.modelrouter.types import ImagePart, ModelParams
from oc8.observability import get_tracer, record_budget_exceeded, record_tool_call
from oc8.realtime.emit import (
    note_focus,
    publish_agent_status,
    publish_run_token_delta,
    publish_run_tool_call,
    record_activity,
)
from oc8.runtime.run_context import append_tool_call
from oc8.runtime.supervision_hook import maybe_checkpoint, maybe_create_anchor
from oc8.skills.runtime import (
    LoadedSkill,
)
from oc8.storage import s3

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 12  # framework default; overridable via settings or per agent


def _max_steps(agent: m.Agent) -> int:
    """Step budget for a run: an agent plugin may raise it per agent for longer,
    multi-record workflows; otherwise the framework setting applies."""
    override = (agent.definition or {}).get("max_steps")
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


def _extract_value(
    arguments: dict[str, Any], value_spec: dict[str, Any] | None = None
) -> float | None:
    """Largest monetary value implied by a tool call — neutral. The connection's
    optional `value_spec` (declared by its plugin) says where a nested/summed
    value lives; the core names no software-specific field."""
    return extract_value(arguments, value_spec)


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


def _authorize(
    agent: m.Agent,
    tc: ToolCall,
    *,
    frame: dict[str, Any],
    delegation_depth: int = 0,
    tool_policies: Mapping[str, ToolPolicy],
    connection_key: str | None,
    tool_scopes: Mapping[str, Any] | None,
    skill_thresholds: Sequence[float | None] = (),
    skill_tool_names: frozenset[str] = frozenset(),
    value_spec: dict[str, Any] | None = None,
    guardrail_attribute_specs: Sequence[dict[str, Any]] = (),
) -> Decision:
    """PEP for a tool call. Every connection tool is decided against the
    department frame (§5.3): which entry governs it is the connection key, and
    which right it needs comes from the connection's `scopes` (unclassified ==
    write, fail-closed). memory_write is gated by the §10 tier policy instead;
    delegate_task (§7) is ALLOW/DENY only -- a delegation carries no monetary
    value. Checks needing the DB (does the target exist, is it in this
    department) live in _delegate, since this function is deliberately pure.

    The frame is bypassed only for tool names that are actually assigned
    skill-invocation tools (`skill_tool_names`) -- never by a `skill_`
    name-prefix match, since MCP tool names flow in unsanitized from a remote
    server and a connection could name a plain tool `skill_anything` to dodge
    the frame check entirely. A stray `skill_`-prefixed tool that isn't one of
    this agent's assigned skills falls through to the normal frame check
    below, exactly like any other tool of that connection."""
    if tc.name in skill_tool_names:
        return Decision(Effect.ALLOW)
    if tc.name == "ask_user":
        return Decision(Effect.ALLOW)
    if tc.name == "propose_change":
        # Like ask_user: it belongs to no connection, so the department frame
        # has nothing to decide it against -- the Assistant's chat run has no
        # tool connection bound at all, and falling through would DENY. That
        # DENY is not enforced (execute_control_tool dispatches control tools
        # before the deny branch and this one never reads `decision`), it is
        # only WRITTEN, so every successful call would be audited as a denial.
        # Deliberate consequence: `is_tenant_assistant`, checked in the
        # dispatch, is then the only gate on this tool -- which is what it
        # should be for a tool that can only ever produce a draft a human has
        # to approve before anything changes.
        return Decision(Effect.ALLOW)
    if tc.name == "decide_approval":
        # Like propose_change and ask_user: it belongs to no connection, so
        # the department frame has nothing to decide it against. Real
        # authorisation for a decision happens where it must, inside
        # `decide_approval` (approvals/service.py) via `_may_apply_the_effect`
        # and `_resolve_agent_actor`'s scope -- this ALLOW only keeps a
        # successful call from being audited as a denial for a tool that was
        # never going to be enforced by this frame in the first place.
        return Decision(Effect.ALLOW)
    if tc.name == "run_shell":
        # Like propose_change and decide_approval, immediately above: it
        # belongs to no connection, so the department frame has nothing to
        # decide it against, and execute_control_tool never reads this
        # decision for run_shell either (see RUN_SHELL's dispatch in
        # control_tools.py, which only checks for a local_result) -- without
        # this special case, a run with no MCP connection bound (only the
        # builtin isolated shell offers run_shell at all -- see
        # internal_agent.py's offer_run_shell) would have every successful
        # call audited as a denial.
        return Decision(Effect.ALLOW)
    if tc.name == "delegate_task":
        if not agent.is_team_lead:
            return Decision(Effect.DENY, "only a team lead can delegate tasks")
        if not str(tc.arguments.get("task_text", "")).strip():
            return Decision(Effect.DENY, "task_text must not be empty")
        raw_target = str(tc.arguments.get("agent_id", ""))
        try:
            target_id = uuid.UUID(raw_target)
        except ValueError:
            return Decision(Effect.DENY, f"invalid agent_id: {raw_target!r}")
        if target_id == agent.id:
            return Decision(Effect.DENY, "an agent cannot delegate to itself")
        if delegation_depth + 1 > MAX_DELEGATION_DEPTH:
            return Decision(Effect.DENY, DEPTH_LIMIT_REASON)
        return Decision(Effect.ALLOW)
    if tc.name == "memory_write":
        content = str(tc.arguments.get("content", ""))
        tier = str(tc.arguments.get("tier", ""))
        if not content.strip():
            return Decision(Effect.DENY, "content must not be empty")
        if len(content) > MAX_MEMORY_CONTENT_LENGTH:
            return Decision(Effect.DENY, f"content exceeds {MAX_MEMORY_CONTENT_LENGTH} characters")
        return authorize_memory_write(frame, agent.narrowing or {}, tier)
    agent_threshold = (agent.presentation or {}).get("approval_value_eur")
    applicable_attributes = [
        spec
        for spec in guardrail_attribute_specs
        if not spec.get("tools") or tc.name in spec["tools"]
    ]
    return authorize_tool_call(
        policies=tool_policies,
        connection_key=connection_key,
        right=required_right(tc.name, tool_scopes),
        tool=tc.name,
        value=_extract_value(tc.arguments, value_spec),
        attributes=extract_attributes(tc.arguments, applicable_attributes),
        extra_thresholds=(
            float(agent_threshold) if agent_threshold is not None else None,
            *skill_thresholds,
        ),
    )


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


async def run_agent(
    db: AsyncSession,
    *,
    agent: m.Agent,
    task_text: str,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    mcp_conn: m.McpConnection | None = None,
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

        model_config: m.ModelConfig | None = None
        if agent.model_config_id is not None:
            model_config = await db.get(m.ModelConfig, agent.model_config_id)
        if model_config is not None:
            provider = model_config.provider
            model = model_config.model
            model_locality = model_config.locality
        else:
            provider = (agent.presentation or {}).get("provider", settings.default_model_provider)
            model = settings.default_model
            model_locality = locality_for_provider(provider)
        # Gate on the CONFIGURED model, not the provider generally -- an
        # agent's model_config is what actually receives the completion
        # request, so that is what decides whether an attached image can be
        # sent along with it. Resolved here, right alongside the same
        # model_config lookup, rather than deep inside build_run_preamble.
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
        tool_policies = effective_tool_policies(frame, agent.narrowing or {})
        if toolset is not None:
            connection_key: str | None = CODING_FRAME_KEY
            tool_scopes: dict[str, Any] | None = {
                right: [n for n, r in CODING_TOOL_RIGHTS.items() if r == right]
                for right in ("read", "modify")
            }
            value_spec: dict[str, Any] | None = None
            focus_spec: dict[str, Any] | None = None
            outward_tools: list[str] | None = None
            guardrail_attribute_specs: list[dict[str, Any]] = []
        elif mcp_conn is not None:
            connection_key = mcp_conn.name
            _cfg = mcp_conn.config if isinstance(mcp_conn.config, dict) else {}
            # The read/write/send classification `required_right` needs lives
            # on the manifest's own ToolPackConnection, not this row's
            # `scopes` column -- that column is an unrelated, list-shaped
            # field (see `resolve_tool_pack_connection`'s docstring). Reading
            # it here used to fail closed to "write" for every tool call
            # whenever the row's `scopes` wasn't itself a dict, which is the
            # common case.
            _manifest_conn = resolve_tool_pack_connection(
                str(_cfg.get("_plugin_name", "")), str(_cfg.get("_connection_key", ""))
            )
            if _manifest_conn is not None and isinstance(_manifest_conn.scopes, dict):
                tool_scopes = _manifest_conn.scopes
            elif isinstance(mcp_conn.scopes, dict):
                # A connection with no manifest (plugin removed from disk, or
                # never plugin-backed at all) that still carries an operator-
                # supplied dict on the row itself -- `CreateMcpConnectionRequest`
                # allows this. Kept as a fallback, not the primary path.
                tool_scopes = mcp_conn.scopes
            else:
                tool_scopes = None
            _vs = _cfg.get("value_spec")
            _fs = _cfg.get("focus_spec")
            _ot = _cfg.get("outward_tools")
            value_spec = _vs if isinstance(_vs, dict) else None
            focus_spec = _fs if isinstance(_fs, dict) else None
            outward_tools = _ot if isinstance(_ot, list) else None
            # `_manifest_conn` (resolved above for `tool_scopes`) also carries
            # this connection's declared `GuardrailAttribute`s -- reused here
            # rather than re-parsing the manifest a second time.
            guardrail_attribute_specs = (
                [
                    {
                        "key": a.key,
                        "datatype": a.datatype,
                        "tools": a.tools,
                        "extract": a.extract,
                    }
                    for a in _manifest_conn.guardrail_attributes
                ]
                if _manifest_conn is not None
                else []
            )
        else:
            connection_key = None
            tool_scopes = None
            value_spec = None
            focus_spec = None
            outward_tools = None
            guardrail_attribute_specs = []
        # A resume leg continues the task its suspended leg opened; see
        # open_run_task. The run is the only place that link is recorded, so a
        # runtime that gets no run_id (a direct run_agent call in a test) simply
        # opens a fresh task, as before.
        resume_task_id: uuid.UUID | None = None
        if run_id is not None:
            run_row = await db.get(m.AgentRun, run_id)
            resume_task_id = run_row.task_id if run_row is not None else None
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
            task_images=task_images,
            supports_vision=supports_vision,
            task=task,
            run_id=run_id,
        )
        messages: list[NeutralMessage] = list(preamble.messages)
        assigned_skills = preamble.assigned_skills
        skill_tool_names = preamble.skill_tool_names
        contains_restricted = preamble.contains_restricted
        has_knowledge = preamble.has_knowledge
        has_instruction_files = preamble.has_instruction_files
        copilot_permissions = preamble.copilot_permissions
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

        async def _live_token_delta(text: str) -> None:
            # No DB write here, unlike _live_tool_call above -- the full text
            # still lands durably once the turn finishes (the transcript
            # entry loop() already appends below), so there is nothing a
            # fresh mid-turn page load would be missing by skipping these
            # fragments. Same no-run no-op as _live_tool_call.
            if run_id is None:
                return
            await publish_run_token_delta(tenant_id, run_id=run_id, text=text)

        async def loop(tools: list[NeutralTool], server: Toolset | None) -> RunResult:
            active_skills: list[LoadedSkill] = []

            def _offered() -> list[NeutralTool]:
                # Shared with the isolated runtime so both offer the same list --
                # see oc8.agent.control_tools.
                return offered_tools(
                    agent,
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

            # Advisory loop-hygiene guard (track_repeat_tool_call, shared with the
            # isolated runtime's /tool endpoint). Per-run, in-memory only: a
            # fresh run_agent call (including a resumed/forked run) starts
            # counting again from zero, an accepted heuristic cost rather than a
            # durable, cross-run counter.
            _repeat_state: dict[str, Any] = {}

            def _track_repeat(tc: ToolCall) -> str | None:
                nonlocal _repeat_state
                _repeat_state, reminder = track_repeat_tool_call(_repeat_state, tc)
                return reminder

            # Per-run, in-memory tool-output budget (cap_tool_output /
            # tool_output_budget_reminder above) -- same "advisory, per-run
            # only" tradeoff as _repeat_state above.
            _tool_output_chars_total = 0
            _tool_output_budget_warned = False

            def _account_tool_output(raw: str) -> tuple[str, str | None]:
                nonlocal _tool_output_chars_total, _tool_output_budget_warned
                capped = cap_tool_output(raw)
                _tool_output_chars_total += len(capped)
                if (
                    _tool_output_budget_warned
                    or _tool_output_chars_total < TOOL_OUTPUT_BUDGET_WARNING_CHARS
                ):
                    return capped, None
                _tool_output_budget_warned = True
                return capped, tool_output_budget_reminder(_tool_output_chars_total)

            steps = 0
            checkpoint_trace_delta: list[dict[str, Any]] = []
            tokens_since_checkpoint = 0
            max_steps = _max_steps(agent)
            # See todo_continuation_reminder: counts auto-continuation rounds
            # separately from `steps` so it can be capped independently of
            # max_steps, even though each round also consumes one step.
            todo_continue_rounds = 0
            for steps in range(1, max_steps + 1):
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

                await dispatch_claude_event(
                    tenant_id,
                    "UserPromptSubmit",
                    user_prompt_submit(prompt=task_text, **_hook_ctx()),
                )

                request_id = uuid.uuid4()
                resolved_messages = messages
                resolved_tools = _offered()
                resolved_params = resolve_params(model_config, agent=agent)
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
                key, cached_result = await cache_flow.lookup(
                    department=department,
                    tenant_id=tenant_id,
                    department_id=agent.department_id,
                    provider=provider,
                    model=model,
                    base_url=resolved_base_url,
                    messages=resolved_messages,
                    tools=resolved_tools,
                    params=resolved_params,
                    contains_restricted=contains_restricted,
                )

                if cached_result is not None:
                    result = cached_result
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

                    async def _complete(
                        sampling_params: ModelParams,
                        req_id: uuid.UUID,
                        msgs: list[NeutralMessage] = resolved_messages,
                        tls: list[NeutralTool] = resolved_tools,
                    ) -> Any:
                        return await accumulate_stream(
                            stream_completion_with_fallback(
                                db,
                                router,
                                tenant_id=tenant_id,
                                agent_id=agent.id,
                                primary=model_config,
                                no_config_provider=provider,
                                no_config_model=model,
                                messages=msgs,
                                tools=tls,
                                params=sampling_params,
                                request_id=req_id,
                                contains_restricted=contains_restricted,
                            ),
                            on_text=_live_token_delta,
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

                    result = await _complete(resolved_params, request_id)
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
                        result = await _complete(
                            bumped_for_length_retry(resolved_params), retry_request_id
                        )
                        await _record(result, retry_request_id)
                        if not result.tool_calls:
                            result.tool_calls = _salvage_tool_calls(result.text, _offered())
                    await cache_flow.store_if_matching(key, result, provider=provider, model=model)
                tokens_since_checkpoint += result.usage.tokens_in + result.usage.tokens_out

                if not result.tool_calls:
                    result.tool_calls = _salvage_tool_calls(result.text, _offered())

                if not result.tool_calls:
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
                        )

                    open_todos = [t for t in todos if t.get("status") != "completed"]
                    if open_todos and todo_continue_rounds < TODO_CONTINUATION_MAX_ROUNDS:
                        # See todo_continuation_reminder: the model tried to finish
                        # while its own checklist still has open items. Append its
                        # (otherwise-dropped) turn plus the reminder and go around
                        # again instead of returning "done" -- bounded on its own
                        # cap, but each round still consumes one `steps` iteration.
                        todo_continue_rounds += 1
                        messages.append(
                            NeutralMessage(role="assistant", content=result.text, tool_calls=[])
                        )
                        messages.append(
                            NeutralMessage(
                                role="user",
                                content=todo_continuation_reminder(
                                    open_todos, todo_continue_rounds
                                ),
                            )
                        )
                        continue

                    # Reaching here with open_todos still set means the round
                    # cap above was hit, not that everything got done -- say so
                    # in the output instead of silently looking like a clean
                    # finish (see todo_continuation_exhausted_note).
                    output_text = result.text
                    if open_todos:
                        output_text = (
                            f"{output_text}\n\n{todo_continuation_exhausted_note(open_todos)}"
                        )

                    task.state = "done"
                    await record_activity(
                        db,
                        tenant_id=tenant_id,
                        agent_id=agent.id,
                        status="warning" if open_todos else "success",
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
                step_trace_start = len(tool_trace)
                for tc in result.tool_calls:
                    decision = _authorize(
                        agent,
                        tc,
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
                            repeat_reminder = _track_repeat(tc)
                            if repeat_reminder is not None:
                                messages.append(
                                    NeutralMessage(role="user", content=repeat_reminder)
                                )
                            checkpoint_trace_delta.append(tool_trace[-1])
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
                                    },
                                    reason_code=decision.reason_code,
                                    reason_context=decision.context,
                                )
                            else:
                                ar = await raise_approval(
                                    db,
                                    tenant_id=tenant_id,
                                    agent_id=agent.id,
                                    task_id=task.id,
                                    action_type="tool_send",
                                    title=f"{agent.name} wants to call {tc.name}",
                                    detail=decision.reason,
                                    payload={"tool": tc.name, "arguments": tc.arguments},
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
                                    "decision": "require_approval",
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
                            )

                        _tool_call_started_at = dt.datetime.now(dt.UTC)
                        # Whether this call is actually dispatched anywhere -- a
                        # control tool, or the tool server. The two branches
                        # below that refuse it before dispatch set this False so
                        # the append site omits the timing keys entirely, the
                        # same as the `pre_hook.blocked` short-circuit above:
                        # `avgToolCallDurationMs` averages calls that RAN, and a
                        # near-zero duration for one that never left the process
                        # would silently drag every denial into that average.
                        _tool_call_dispatched = True
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
                        )
                        if control is not None:
                            # A core-owned tool (memory/ask/delegate/skill). The
                            # dispatcher is shared with the isolated runtime and
                            # deliberately mutates nothing here -- it reports, we
                            # store, because the two runtimes keep this state in
                            # different places. See oc8.agent.control_tools.
                            output = control.output
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
                            if control.suspend == "waiting_for_input":
                                task.state = "waiting_for_input"
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
                                )
                        elif decision.effect is Effect.DENY or server is None:
                            output = f"ERROR: {decision.reason or 'no tool server available'}"
                            _tool_call_dispatched = False
                        elif (
                            target := outward_target(
                                tc.name, tc.arguments, focus_spec, outward_tools
                            )
                        ) is not None and await already_delivered(
                            db, tenant_id=tenant_id, task_id=task.id, target=target
                        ):
                            # Before the call, not after: the point is that the
                            # recipient is not reached twice, and a check that ran
                            # afterwards could only report it.
                            output = REFUSAL.format(target=target)
                            _tool_call_dispatched = False
                        else:
                            # Live-log which record the agent is working on, from
                            # the connection's own focus_spec (a plugin supplies
                            # it; the core names nothing software-specific).
                            focus = describe_focus(tc.name, tc.arguments, focus_spec)
                            if focus is not None:
                                await note_focus(
                                    db,
                                    tenant_id=tenant_id,
                                    agent_id=agent.id,
                                    task_id=task.id,
                                    focus=focus,
                                    specific=describes_a_record(tc.name, tc.arguments, focus_spec),
                                    cache_hit=cached_result is not None,
                                )
                            try:
                                output = await server.call(tc.name, tc.arguments)
                            except Exception as exc:  # surface tool errors to the model
                                output = f"ERROR: {exc}"
                            if target is not None and not output.startswith("ERROR:"):
                                await remember_delivery(
                                    db,
                                    tenant_id=tenant_id,
                                    task_id=task.id,
                                    target=target,
                                )
                        output, tool_output_budget_note = _account_tool_output(output)
                        _tool_call_entry: dict[str, Any] = {
                            "tool": tc.name,
                            "arguments": tc.arguments,
                            "result": output[:300],
                        }
                        if _tool_call_dispatched:
                            _tool_call_entry["startedAt"] = _tool_call_started_at.isoformat()
                            _tool_call_entry["durationMs"] = int(
                                (dt.datetime.now(dt.UTC) - _tool_call_started_at).total_seconds()
                                * 1000
                            )
                        tool_trace.append(_tool_call_entry)
                        await _live_tool_call(tool_trace[-1])
                        messages.append(
                            NeutralMessage(
                                role="tool", content=output, tool_call_id=tc.id, name=tc.name
                            )
                        )
                        repeat_reminder = _track_repeat(tc)
                        if repeat_reminder is not None:
                            messages.append(NeutralMessage(role="user", content=repeat_reminder))
                        if tool_output_budget_note is not None:
                            messages.append(
                                NeutralMessage(role="user", content=tool_output_budget_note)
                            )
                        checkpoint_trace_delta.append(tool_trace[-1])
                        post_event = (
                            "PostToolUseFailure" if output.startswith("ERROR:") else "PostToolUse"
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

                if cached_result is None and any(
                    str(t.get("result", "")).startswith("ERROR:")
                    for t in tool_trace[step_trace_start:]
                ):
                    await cache_flow.invalidate(key)

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
            )

        async def _run_with_session_end() -> RunResult:
            try:
                if toolset is not None:
                    return await loop(toolset.tools, toolset)
                if mcp_conn is not None:
                    cfg = mcp_conn.config or {}
                    env = await resolve_mcp_env(
                        db, tenant_id=tenant_id, cfg=cfg, connection_name=mcp_conn.name
                    )
                    command, args = wrap_with_requirements(
                        cfg.get("command", ""), cfg.get("args", []), cfg
                    )
                    async with McpSession(command, args, env=env) as server:
                        return await loop(apply_tool_notes(server.tools, cfg), server)
                return await loop([], None)
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
