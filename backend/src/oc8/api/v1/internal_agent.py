"""Control-plane internal API for an ISOLATED agent run (§8.1/§8.3).

An agent runs as a thin, stateless shell in its own container. It holds NO
secrets — not a provider key, not an MCP credential, not the secret-store KEK.
Every privileged step is done HERE, in the trusted control plane, and driven by
the shell over these endpoints with a short-lived, run-scoped agent token:

- POST /internal/agent/step  -> one model turn (Model Router holds the keys)
- POST /internal/agent/tool  -> authorize + execute one tool (creds resolved here)
- POST /internal/agent/finish-> record the terminal result

The run transcript lives in the run row (control plane), so the endpoints are
stateless and the container carries only the loop, never the data.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from oc8 import models as m
from oc8.agent import cache_flow
from oc8.agent.control_tools import (
    CONTROL_TOOL_NAMES,
    execute_control_tool,
    offered_tools,
)
from oc8.agent.engine import (
    TODO_CONTINUATION_MAX_ROUNDS,
    _authorize,
    _max_steps,
    todo_continuation_exhausted_note,
    todo_continuation_reminder,
)
from oc8.agent.harness.calls import call_sig as _call_sig
from oc8.agent.harness.stages.c_reminders import (
    TOOL_OUTPUT_BUDGET_WARNING_CHARS,
    cap_tool_output,
    tool_output_budget_reminder,
    track_repeat_tool_call,
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
from oc8.agent.tool_idempotency import record_invocation, replayed_result
from oc8.agent.tool_notes import apply_tool_notes
from oc8.agent.tool_semantics import describe_focus, describes_a_record
from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.approvals import raise_approval
from oc8.audit import append_event
from oc8.authz.pdp import (
    Decision,
    Effect,
    effective_tool_policies,
    required_right,
)
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.config import get_settings
from oc8.metering import record_usage
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
from oc8.modelrouter.types import ImagePart, ModelParams, TextPart
from oc8.realtime.emit import note_focus, publish_run_token_delta, publish_run_tool_call
from oc8.runtime.approval_resume import pre_decided_map
from oc8.runtime.registry import BUILTIN_ISOLATED_RUNTIME_REF
from oc8.runtime.run_context import append_tool_call
from oc8.skills.runtime import load_assigned_skills
from oc8.storage import s3

router = APIRouter()

RUN_SCOPE = "run:"


async def _run_for_token(
    run_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> m.AgentRun:
    """A run the calling agent token is scoped to. The token is kind=agent and
    carries `run:<id>` in its scopes; anything else is refused, so an isolated
    shell can only ever touch its own run."""
    if principal.kind != "agent" or f"{RUN_SCOPE}{run_id}" not in (principal.scopes or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token not scoped to this run")
    run = await db.get(m.AgentRun, run_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run


# --------------------------------------------------------------------- serde


def _content_to_json(content: str | list[Any]) -> str | list[dict[str, Any]]:
    """`NeutralMessage.content` round-trips through the run's JSONB `context`
    between every step of this endpoint's split /step + /tool request cycle
    (there is no in-memory loop here to hold it across calls, unlike the
    in-process engine). A list `content` can hold an `ImagePart`, whose `data`
    is raw `bytes` -- not JSON-serializable, and neither is the dataclass
    itself -- so it must become a plain, JSON-safe dict here before `ctx` is
    committed, and be reversed by `_content_from_json` below."""
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, ImagePart):
            parts.append(
                {
                    "type": "image",
                    "data": base64.b64encode(part.data).decode(),
                    "content_type": part.content_type,
                }
            )
        else:
            parts.append({"type": "text", "text": part.text})
    return parts


def _content_from_json(content: Any) -> str | list[Any]:
    if isinstance(content, list):
        parts: list[Any] = []
        for p in content:
            if p.get("type") == "image":
                parts.append(
                    ImagePart(data=base64.b64decode(p["data"]), content_type=p["content_type"])
                )
            else:
                parts.append(TextPart(text=p.get("text", "")))
        return parts
    return str(content or "")


def _to_messages(raw: list[dict[str, Any]]) -> list[NeutralMessage]:
    out: list[NeutralMessage] = []
    for d in raw:
        out.append(
            NeutralMessage(
                role=d["role"],
                content=_content_from_json(d.get("content", "")),
                tool_calls=[
                    ToolCall(id=t["id"], name=t["name"], arguments=t.get("arguments", {}))
                    for t in d.get("tool_calls", [])
                ],
                tool_call_id=d.get("tool_call_id"),
                name=d.get("name"),
            )
        )
    return out


def _from_message(msg: NeutralMessage) -> dict[str, Any]:
    d: dict[str, Any] = {"role": msg.role, "content": _content_to_json(msg.content)}
    if msg.tool_calls:
        d["tool_calls"] = [
            {"id": t.id, "name": t.name, "arguments": t.arguments} for t in msg.tool_calls
        ]
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    if msg.name:
        d["name"] = msg.name
    return d


async def _load(
    db: DbSession, run: m.AgentRun
) -> tuple[m.Agent, m.Department | None, m.McpConnection | None]:
    agent = await db.get(m.Agent, run.agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    dept = await db.get(m.Department, agent.department_id)
    conn = None
    mcp_id = run.context.get("mcp_connection_id")
    if mcp_id:
        conn = await db.get(m.McpConnection, uuid.UUID(str(mcp_id)))
    elif dept is not None:
        from sqlalchemy import select

        conn = (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.department_id == dept.id,
                    m.McpConnection.connected.is_(True),
                )
                .order_by(m.McpConnection.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    return agent, dept, conn


def _mcp_params(conn: m.McpConnection) -> dict[str, Any]:
    cfg = conn.config if isinstance(conn.config, dict) else {}
    return cfg


def _manifest_scopes(conn: m.McpConnection | None) -> dict[str, Any] | None:
    """The read/write/send classification `required_right` needs, resolved
    from `conn`'s plugin manifest -- NOT `conn.scopes` itself, which is an
    unrelated, list-shaped DB column that happens to share the name (see
    `resolve_tool_pack_connection`'s docstring). Falls back to `conn.scopes`
    only for a connection with no matching manifest that still carries an
    operator-supplied dict there directly.
    """
    if conn is None:
        return None
    cfg = _mcp_params(conn)
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is not None and isinstance(manifest_conn.scopes, dict):
        return manifest_conn.scopes
    return conn.scopes if isinstance(conn.scopes, dict) else None


async def _mcp_env(conn: m.McpConnection, db: DbSession, tenant_id: uuid.UUID) -> dict[str, str]:
    return await resolve_mcp_env(
        db, tenant_id=tenant_id, cfg=_mcp_params(conn), connection_name=conn.name
    )


# --------------------------------------------------------------------- step


class StepResult(BaseModel):
    done: bool
    text: str = ""
    tool_calls: list[dict[str, Any]] = []
    #: Set only when the model was truncated by its token budget without
    #: producing an answer or a tool call, even after a retry with a bumped
    #: budget (see engine.py's identical check) -- the shell must report this
    #: verbatim as the run's terminal status instead of its own done/no-calls
    #: heuristic, which would otherwise read a truncation as "the agent
    #: finished".
    status_override: str | None = None


@router.post(
    "/internal/agent/{run_id}/step",
    response_model=StepResult,
    dependencies=[Depends(unguarded("run-scoped agent token, verified by the route itself"))],
)
async def step(
    run_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> StepResult:
    run = await _run_for_token(run_id, db, principal)
    agent, dept, conn = await _load(db, run)

    ctx = dict(run.context)

    # Same step-limit as the in-process engine's `for steps in range(1,
    # max_steps + 1)` loop bound (§8.3, engine.py's _max_steps): once this
    # many completions have already happened, refuse to place another one
    # rather than letting the shell keep calling this endpoint until its own
    # MAX_ITERS backstop (isolated_shell.py) -- that backstop is deliberately
    # far above any real budget and must never be the thing that actually
    # stops a run. No model call, no cost, on this path -- ctx["steps"] isn't
    # incremented here, so a resumed/retried request stays idempotent.
    if int(ctx.get("steps", 0)) >= _max_steps(agent):
        return StepResult(done=True, text="Reached step limit.", status_override="done")

    transcript: list[dict[str, Any]] = list(ctx.get("transcript", []))
    tool_schemas_raw: list[dict[str, Any]] = list(ctx.get("tool_schemas", []))

    # Resolved BEFORE seeding, because the preamble's KB retrieval needs the
    # locality to decide what may leave the tenant's region.
    settings = get_settings()
    model_config = (
        await db.get(m.ModelConfig, agent.model_config_id) if agent.model_config_id else None
    )
    if model_config is not None:
        provider, model = model_config.provider, model_config.model
    else:
        provider = (agent.presentation or {}).get("provider", settings.default_model_provider)
        model = settings.default_model
    model_locality = locality_for_provider(provider)

    frame = dept.frame if dept is not None else {}
    assigned_skills = await load_assigned_skills(db, agent=agent, tenant_id=run.tenant_id)

    if not transcript:
        # Seed the SAME context the in-process engine seeds -- memory, KB, roster,
        # skills catalog -- via the shared preamble. Seeding only the system prompt
        # (as this endpoint used to) left an isolated agent unable to name a
        # colleague to delegate to or a skill to invoke.
        #
        # Task 7 (chat/instruction-file-attachments): this endpoint calls
        # build_run_preamble directly rather than duplicating its logic, so
        # threading task_images/supports_vision through is a one-line addition
        # here, not a second copy of the branch in preamble.py. This IS the
        # second real call site the in-process engine.py doesn't cover --
        # DockerIsolatedRuntime.execute (runtime/isolated.py) never calls
        # build_run_preamble itself, it just forwards run.context (which
        # already carries "task_images") to the container, which lands here.
        task_images_raw = ctx.get("task_images", [])
        task_images = [
            ImagePart(
                data=await s3.get_object(entry["bucket_key"]),
                content_type=entry["content_type"],
            )
            for entry in task_images_raw
        ]
        supports_vision = (
            bool(model_config.params.get("supports_vision", False))
            if model_config is not None
            else False
        )
        task_row = await db.get(m.Task, run.task_id) if run.task_id is not None else None
        preamble = await build_run_preamble(
            db,
            agent=agent,
            tenant_id=run.tenant_id,
            task_text=str(ctx.get("task", "")),
            frame=frame,
            model_locality=model_locality,
            task_images=task_images,
            supports_vision=supports_vision,
            task=task_row,
            run_id=run_id,
        )
        transcript = [_from_message(msg) for msg in preamble.messages]
        assigned_skills = preamble.assigned_skills
        # Persisted like transcript/tool_schemas/active_skill_ids: the preamble
        # only runs on a run's FIRST step, so without this every later step of
        # the same run would have to guess -- and it is what forces a restricted
        # run to a local model and keeps it out of the department cache.
        ctx["contains_restricted"] = preamble.contains_restricted
        ctx["has_knowledge"] = preamble.has_knowledge
        ctx["has_instruction_files"] = preamble.has_instruction_files
        ctx["copilot_permissions"] = sorted(preamble.copilot_permissions)
        if conn is not None and not tool_schemas_raw:
            cfg = _mcp_params(conn)
            env = await _mcp_env(conn, db, run.tenant_id)
            command, args = wrap_with_requirements(cfg.get("command", ""), cfg.get("args", []), cfg)
            async with McpSession(command, args, env=env) as s:
                tool_schemas_raw = [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in apply_tool_notes(s.tools, cfg)
                ]
        ctx["tool_schemas"] = tool_schemas_raw

    mcp_tools = [
        NeutralTool(
            name=t["name"],
            description=t.get("description", ""),
            parameters=t.get("parameters", {"type": "object", "properties": {}}),
        )
        for t in tool_schemas_raw
    ]
    # Active skills live in the run context, not in a local variable: every step is
    # a separate request with a fresh session, so there is no loop to hold them.
    active_ids = {str(s) for s in ctx.get("active_skill_ids", [])}
    active_skills = [s for s in assigned_skills if str(s.skill_version_id) in active_ids]
    # Read back what the preamble determined on the FIRST step. Defaulting to
    # False keeps a run already in flight (whose ctx predates this) working.
    contains_restricted = bool(ctx.get("contains_restricted", False))
    has_knowledge = bool(ctx.get("has_knowledge", False))
    has_instruction_files = bool(ctx.get("has_instruction_files", False))
    copilot_permissions = frozenset(ctx.get("copilot_permissions", []))

    # This endpoint only ever runs for a containerized runtime (the in-process
    # engine calls offered_tools directly, never over HTTP). A real runtime
    # plugin (e.g. claude_code_runtime) has its own local file tools, so only
    # offer write_output_file -- and, for the same reason, run_shell -- for
    # the builtin isolated shell, which has none of its own.
    offer_write_output_file = (
        not agent.runtime_ref or agent.runtime_ref == BUILTIN_ISOLATED_RUNTIME_REF
    )
    tools = offered_tools(
        agent,
        assigned_skills=assigned_skills,
        active_skills=active_skills,
        mcp_tools=mcp_tools,
        has_knowledge=has_knowledge,
        has_instruction_files=has_instruction_files,
        copilot_permissions=copilot_permissions,
        offer_write_output_file=offer_write_output_file,
        offer_run_shell=offer_write_output_file,
    )

    resolved_tools = tools
    # Shared with the in-process engine so sampling cannot drift between the
    # two runtimes -- see oc8.modelrouter.sampling.
    resolved_params = resolve_params(model_config, agent=agent)
    # Must match what fallback.py's own base_url resolution will actually
    # send for this provider (params override, else the tenant's bound
    # credential) -- see agent/engine.py's identical comment.
    resolved_base_url = (
        (model_config.params or {}).get("base_url") if model_config is not None else None
    ) or await resolve_model_base_url(
        db,
        tenant_id=run.tenant_id,
        provider=provider,
        credential_id=model_config.credential_id if model_config is not None else None,
    )

    async def _live_token_delta(text: str) -> None:
        # Same Live Log parity as the in-process engine's own callback
        # (agent/engine.py's _live_token_delta) -- transient, no DB write,
        # since the full text still lands durably below once the turn
        # completes (ctx["transcript"] + the final db.commit()).
        await publish_run_token_delta(run.tenant_id, run_id=run.id, text=text)

    async def _complete(
        msgs: list[NeutralMessage], sampling_params: ModelParams, req_id: uuid.UUID
    ) -> Any:
        return await accumulate_stream(
            stream_completion_with_fallback(
                db,
                get_model_router(),
                tenant_id=run.tenant_id,
                agent_id=agent.id,
                primary=model_config,
                no_config_provider=provider,
                no_config_model=model,
                messages=msgs,
                tools=resolved_tools,
                params=sampling_params,
                request_id=req_id,
                contains_restricted=contains_restricted,
            ),
            on_text=_live_token_delta,
        )

    async def _record(res: Any, req_id: uuid.UUID) -> None:
        await record_usage(
            db,
            tenant_id=run.tenant_id,
            request_id=req_id,
            model=res.model,
            provider=res.provider,
            tokens_in=res.usage.tokens_in,
            tokens_out=res.usage.tokens_out,
            agent_id=agent.id,
            department_id=agent.department_id,
        )

    # See engine.py's todo_continuation_reminder / todo_continue_rounds: a
    # continuation round never crosses a /step HTTP call here -- the shell
    # must never see an intermediate "no tool calls yet" response, since its
    # own loop protocol (isolated_shell.py) has no "keep going anyway" path
    # and would just end the run. So the whole nudge-and-retry cycle happens
    # in this one call via this internal loop, mirroring the length-retry
    # precedent just below (which already does 2 completions per /step
    # call) instead of a second completion. A plain local counter is
    # correct here (never reset mid-cap, never spans calls) precisely
    # because it never has to survive past this one request.
    todo_continue_rounds = 0
    while True:
        resolved_messages = _to_messages(transcript)
        request_id = uuid.uuid4()
        # Department prompt caching, through the SAME helper the in-process engine
        # uses (oc8.agent.cache_flow) -- an isolated deployment must not silently
        # render a settings toggle and a savings figure that do nothing.
        key, cached_result = await cache_flow.lookup(
            department=dept,
            tenant_id=run.tenant_id,
            department_id=agent.department_id,
            provider=provider,
            model=model,
            base_url=resolved_base_url,
            messages=resolved_messages,
            tools=resolved_tools,
            params=resolved_params,
            contains_restricted=contains_restricted,
        )
        # Every model turn is metered HERE, because this is where an isolated run's
        # turns happen -- the container holds no keys and never calls a provider. Same
        # record the in-process engine writes after its own turn (§15.3): without it a
        # deployment on OC8_AGENT_ISOLATION=true bills nothing and its budgets never
        # fill, so the runtime's budget gate could never fire.
        if cached_result is not None:
            result = cached_result
            # Nothing new was stored this step -- a leftover key from an earlier
            # step must not be invalidated by a LATER step's tool failure (see
            # the pop below).
            ctx.pop("pending_cache_key", None)
            await record_usage(
                db,
                tenant_id=run.tenant_id,
                request_id=request_id,
                model=result.model,
                provider=result.provider,
                tokens_in=0,
                tokens_out=0,
                agent_id=agent.id,
                department_id=agent.department_id,
                cache_hit=True,
                saved_tokens_in=result.usage.tokens_in,
                saved_tokens_out=result.usage.tokens_out,
            )
        else:
            result = await _complete(resolved_messages, resolved_params, request_id)
            await _record(result, request_id)
            if result.stop_reason == "length" and not result.tool_calls and not result.text.strip():
                # See engine.py's identical check: a reasoning-capable model can
                # spend its whole completion budget on hidden reasoning and hit
                # max_tokens before writing anything visible. One retry with
                # double the budget, before this silently reads as the run being
                # finished with nothing actually done.
                retry_request_id = uuid.uuid4()
                result = await _complete(
                    resolved_messages, bumped_for_length_retry(resolved_params), retry_request_id
                )
                await _record(result, retry_request_id)
            await cache_flow.store_if_matching(key, result, provider=provider, model=model)
            # Read by /tool below, once this step's requested tool calls come back
            # and any of them turns out to have failed for real (see that
            # endpoint's own invalidate call) -- store_if_matching can't know that
            # yet, since it runs before any tool call this completion requested
            # has executed. Same fix as agent/engine.py's step loop, adapted to
            # this endpoint's split /step + /tool request cycle: there is no
            # single in-process loop here to hold the key across both calls, so
            # it travels on the run's own context instead.
            ctx["pending_cache_key"] = key

        transcript.append(
            _from_message(
                NeutralMessage(role="assistant", content=result.text, tool_calls=result.tool_calls)
            )
        )
        ctx["steps"] = int(ctx.get("steps", 0)) + 1

        truncated_empty = (
            result.stop_reason == "length" and not result.tool_calls and not result.text.strip()
        )
        if not result.tool_calls and not truncated_empty:
            # See engine.py's identical gate: the model tried to finish while
            # its own todo_write checklist still has open items.
            open_todos = [t for t in ctx.get("todos", []) if t.get("status") != "completed"]
            if (
                open_todos
                and todo_continue_rounds < TODO_CONTINUATION_MAX_ROUNDS
                and int(ctx["steps"]) < _max_steps(agent)
            ):
                todo_continue_rounds += 1
                transcript.append(
                    _from_message(
                        NeutralMessage(
                            role="user",
                            content=todo_continuation_reminder(open_todos, todo_continue_rounds),
                        )
                    )
                )
                continue
        break

    ctx["transcript"] = transcript
    run.context = ctx
    await db.commit()

    # Reaching here with no tool call and open todos means the round cap (or
    # the step budget) was hit, not that everything got done -- say so in the
    # output instead of silently looking like a clean finish, same as
    # engine.py's identical check (see todo_continuation_exhausted_note).
    step_text = result.text
    if not result.tool_calls and not truncated_empty:
        open_todos_final = [t for t in ctx.get("todos", []) if t.get("status") != "completed"]
        if open_todos_final:
            step_text = f"{step_text}\n\n{todo_continuation_exhausted_note(open_todos_final)}"
    elif truncated_empty:
        # Otherwise this ends as status_override="failed" with an empty
        # text, the shell forwards that empty text to /finish verbatim, and
        # the run closes with no diagnosable reason anywhere -- see
        # engine.py's identical check, whose RunResult.output already carries
        # this same message for the in-process path.
        step_text = "Model exceeded its token budget without producing an answer or tool call."

    return StepResult(
        done=not result.tool_calls and int(ctx["steps"]) <= _max_steps(agent),
        text=step_text,
        tool_calls=[
            {"id": t.id, "name": t.name, "arguments": t.arguments} for t in result.tool_calls
        ],
        # Truncated even after the retry above -- never let the shell read
        # this as "done" (see engine.py's identical check for why).
        status_override="failed" if truncated_empty else None,
    )


# --------------------------------------------------------------------- tool


class ToolBody(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = {}
    #: Present only for run_shell: isolated_shell.py already executed the
    #: command locally before this POST (see its module docstring) -- this is
    #: the already-computed result to record, not to execute.
    local_result: dict[str, Any] | None = None


class ToolResult(BaseModel):
    status: str  # ok | denied | waiting_for_approval | waiting_for_input
    output: str = ""


@router.post(
    "/internal/agent/{run_id}/tool",
    response_model=ToolResult,
    dependencies=[Depends(unguarded("run-scoped agent token, verified by the route itself"))],
)
async def tool(
    run_id: uuid.UUID,
    body: ToolBody,
    db: DbSession,
    principal: CurrentPrincipal,
) -> ToolResult:
    run = await _run_for_token(run_id, db, principal)
    agent, dept, conn = await _load(db, run)
    tc = ToolCall(id=body.id, name=body.name, arguments=body.arguments)

    assigned_skills = await load_assigned_skills(db, agent=agent, tenant_id=run.tenant_id)
    skill_tool_names = frozenset(s.tool_name for s in assigned_skills)
    is_control_call = tc.name in CONTROL_TOOL_NAMES or tc.name in skill_tool_names
    # A core-owned tool (remember/ask/delegate/invoke a skill) is executed by the
    # control plane itself and needs no MCP connection. Requiring one here made
    # every one of them unusable on a run without a connection.
    if conn is None and not is_control_call:
        raise HTTPException(status.HTTP_409_CONFLICT, "no tool connection bound to this run")

    frame = dept.frame if dept is not None else {}
    cfg = _mcp_params(conn) if conn is not None else {}
    value_spec = cfg.get("value_spec") if isinstance(cfg.get("value_spec"), dict) else None
    focus_spec = cfg.get("focus_spec") if isinstance(cfg.get("focus_spec"), dict) else None
    outward_tools = cfg.get("outward_tools") if isinstance(cfg.get("outward_tools"), list) else None
    scopes = _manifest_scopes(conn)

    active_ids = {str(s) for s in run.context.get("active_skill_ids", [])}
    active_skills = [s for s in assigned_skills if str(s.skill_version_id) in active_ids]

    decision = _authorize(
        agent,
        tc,
        frame=frame,
        # Both were missing on this path. Without skill_tool_names an assigned
        # skill tool falls through to the frame check and is denied; without
        # delegation_depth the §7 cap always compared against 0, so a delegation
        # chain could run past MAX_DELEGATION_DEPTH.
        skill_tool_names=skill_tool_names,
        delegation_depth=int(run.context.get("delegation_depth", 0)),
        skill_thresholds=tuple(
            g.gt
            for s in active_skills
            for g in s.definition.guardrails
            if g.type == "value_threshold" and g.then == "require_approval"
        ),
        tool_policies=effective_tool_policies(frame, agent.narrowing or {}),
        connection_key=conn.name if conn is not None else None,
        tool_scopes=scopes,
        value_spec=value_spec,
    )
    # Honour an operator's earlier decision on this exact call (resume).
    if decision.effect is Effect.REQUIRE_APPROVAL:
        verdict = pre_decided_map(run.context.get("resolved_tool_approvals", [])).get(_call_sig(tc))
        if verdict == "approve":
            decision = Decision(Effect.ALLOW, "operator approved")
        elif verdict == "reject":
            decision = Decision(Effect.DENY, "operator rejected this action")

    await append_event(
        db,
        tenant_id=run.tenant_id,
        actor_type="agent",
        actor_id=agent.id,
        category="tool_action",
        action=f"tool.call:{tc.name}",
        resource={"tool": tc.name, "arguments": tc.arguments},
        decision=decision.effect.value,
        reason=decision.reason or None,
        originating_operator=run.context.get("originating_operator"),
    )

    if decision.effect is Effect.REQUIRE_APPROVAL:
        ar = await raise_approval(
            db,
            tenant_id=run.tenant_id,
            agent_id=agent.id,
            task_id=run.task_id,
            action_type="tool_send",
            title=f"{agent.name} wants to call {tc.name}",
            detail=decision.reason,
            payload={"tool": tc.name, "arguments": tc.arguments},
        )
        # Record the suspend verdict so the isolated runtime maps the run to
        # waiting_for_approval after the container exits.
        run.context = {
            **run.context,
            "isolated_result": {"status": "waiting_for_approval", "output": decision.reason or ""},
        }
        await db.commit()
        from oc8.realtime.bus import get_event_bus

        await get_event_bus().publish_event(
            run.tenant_id,
            "approval.created",
            {
                "approval_id": str(ar.id),
                "action_type": ar.action_type,
                "status": ar.status,
                # title/detail travel in the envelope so the push hook never
                # has to re-read this row from a fresh, uncommitted-blind
                # session (see EventBus._push_payload).
                "title": ar.title,
                "detail": ar.detail,
            },
            source=f"oc8/approval/{ar.id}",
        )
        return ToolResult(status="waiting_for_approval", output=decision.reason or "")

    ctx = dict(run.context)
    suspend: str | None = None

    # A core-owned tool goes through the SAME dispatcher as the in-process engine
    # (oc8.agent.control_tools). It returns None for a connection tool, which then
    # falls through to the MCP server below.
    task = await db.get(m.Task, run.task_id) if run.task_id is not None else None
    # Tool-call timing, at parity with the in-process engine (agent/engine.py's
    # own `_tool_call_started_at`/`_tool_call_dispatched` pair, same key names
    # and same millisecond unit). Without this the container/isolated runtime
    # writes `context->'toolCalls'` entries with no `durationMs` at all, and
    # every agent on that runtime reports `avgToolCallDurationMs: null` forever
    # -- the "container parity is not automatic" trap, since the two runtimes
    # rebuild this append independently.
    started_at = dt.datetime.now(dt.UTC)
    #: False on the branches below that refuse the call before it is dispatched
    #: anywhere; those entries omit both timing keys rather than record a
    #: near-zero duration for a call that never ran.
    dispatched = True
    control = (
        await execute_control_tool(
            db,
            tenant_id=run.tenant_id,
            agent=agent,
            task=task,
            tc=tc,
            decision=decision,
            assigned_skills=assigned_skills,
            active_skills=active_skills,
            mcp_conn=conn,
            originating_operator=run.context.get("originating_operator"),
            run_id=run.id,
            local_result=body.local_result,
        )
        if task is not None
        else None
    )

    if control is not None:
        output = control.output
        suspend = control.suspend
        if control.pending_run is not None:
            # The executor publishes it after committing -- never this request: the
            # worker could otherwise read a run whose row isn't durably visible.
            ctx["pending_runs"] = [*ctx.get("pending_runs", []), str(control.pending_run)]
        if control.activated_skill is not None:
            # There is no loop here to hold the activation, so it lives on the run
            # and the next /step recomputes the narrowed tool list from it.
            ctx["active_skill_ids"] = [
                *ctx.get("active_skill_ids", []),
                str(control.activated_skill.skill_version_id),
            ]
            # Only the active-set is tracked here (it drives tool narrowing on the
            # next /step). The skill's procedure travels in the tool result itself,
            # so no extra message is injected -- see execute_control_tool.
        if control.rendered_component is not None:
            # Durable copy first -- an unattended run (chat/cron) has no live
            # viewer to catch the WS-only event below, so this is the only
            # copy that survives past the moment it fired (see GET /runs/{id}).
            ctx["rendered_components"] = [
                *ctx.get("rendered_components", []),
                control.rendered_component,
            ]
            # Unlike pending_run above, this event carries its whole payload
            # inline (run_id + props) -- no consumer needs to look up a row
            # that isn't committed yet, so publishing before the request's
            # terminal commit is safe here.
            from oc8.realtime.bus import get_event_bus

            await get_event_bus().publish_event(
                run.tenant_id,
                "run.component_rendered",
                {"run_id": str(run.id), **control.rendered_component},
                source=f"oc8/run/{run.id}",
            )
        if control.todos is not None:
            # Whole-list replace, unlike rendered_components above -- this IS
            # the current state, not a log of every call. See RunResult.todos.
            ctx["todos"] = control.todos
            # Live tab open on this run right now: without this, the frontend's
            # `useRun` cache only ever sees the todos captured at its initial GET
            # fetch (no polling, everything else after that is WS-patched) -- an
            # already-open Live Log would show a stale or empty list until the
            # viewer navigated away and back. Same "durable copy + live event"
            # split as rendered_component above.
            from oc8.realtime.bus import get_event_bus

            await get_event_bus().publish_event(
                run.tenant_id,
                "run.todos_updated",
                {"run_id": str(run.id), "todos": control.todos},
                source=f"oc8/run/{run.id}",
            )
    elif decision.effect is Effect.DENY:
        output = f"ERROR: {decision.reason or 'denied'}"
        dispatched = False
    elif conn is None:
        output = "ERROR: no tool server available"
        dispatched = False
    elif (
        (
            target := outward_target(
                tc.name, tc.arguments, focus_spec, outward_tools
            )
        )
        is not None
        and run.task_id is not None
        and await already_delivered(db, tenant_id=run.tenant_id, task_id=run.task_id, target=target)
    ):
        # Checked before the call, not after: the point is that the recipient is
        # not reached twice, and a check that ran afterwards could only report it.
        output = REFUSAL.format(target=target)
        dispatched = False
    else:
        focus = describe_focus(tc.name, tc.arguments, focus_spec)
        if focus is not None:
            await note_focus(
                db,
                tenant_id=run.tenant_id,
                agent_id=agent.id,
                task_id=run.task_id,
                focus=focus,
                specific=describes_a_record(tc.name, tc.arguments, focus_spec),
            )
        # Idempotency (§8.7 R5), and only for calls that CHANGE something: a
        # restarted task replaying a write must get the first result rather than
        # act twice. Reads are exempt on purpose -- deduplicating a search would
        # hide the very changes the agent is meant to observe.
        writes = required_right(tc.name, scopes) != "read"
        replay = (
            await replayed_result(
                db,
                tenant_id=run.tenant_id,
                task_id=run.task_id,
                tool=tc.name,
                arguments=tc.arguments,
            )
            if writes and run.task_id is not None
            else None
        )
        if replay is not None:
            output = replay
        else:
            try:
                # Resolved here, inside the guard and after the replay check:
                # an OAuth-backed connection MINTS a token in this call, so it
                # can fail on its own -- and that failure belongs to the model
                # as a tool error, exactly like an unreachable bridge. A
                # replayed write needs no bridge and now mints nothing.
                env = await _mcp_env(conn, db, run.tenant_id)
                command, args = wrap_with_requirements(
                    cfg.get("command", ""), cfg.get("args", []), cfg
                )
                async with McpSession(command, args, env=env) as s:
                    output = await s.call(tc.name, tc.arguments)
            except Exception as exc:  # surface to the model
                output = f"ERROR: {exc}"
            # Only a successful side effect is worth replaying. Recording a failure
            # would answer a legitimate retry with the old error forever.
            if not output.startswith("ERROR:"):
                if target is not None and run.task_id is not None:
                    await remember_delivery(
                        db, tenant_id=run.tenant_id, task_id=run.task_id, target=target
                    )
                if writes and run.task_id is not None:
                    await record_invocation(
                        db,
                        tenant_id=run.tenant_id,
                        task_id=run.task_id,
                        tool=tc.name,
                        arguments=tc.arguments,
                        result=output,
                    )

    # Stopped HERE, the moment the call itself returned -- not at the append
    # site far below, which is separated from it by the transcript rewrite and
    # a `db.flush()` whose time is this request's, not the tool's.
    duration_ms = int((dt.datetime.now(dt.UTC) - started_at).total_seconds() * 1000)

    # This tool call belongs to the completion /step just cached (see its own
    # ctx["pending_cache_key"] comment) -- a real failure here means that
    # completion's first-try guess was wrong, so it must not replay verbatim
    # on every identical future trigger for the rest of the cache TTL.
    if output.startswith("ERROR:") and ctx.get("pending_cache_key") is not None:
        await cache_flow.invalidate(ctx["pending_cache_key"])

    # Tool-output budget (cap_tool_output / tool_output_budget_reminder), at
    # parity with the in-process engine's own `_account_tool_output`
    # (engine.py's loop()) -- same cumulative counter, just persisted on
    # run.context instead of a local closure variable, same reasoning as
    # repeat_tracker below.
    output = cap_tool_output(output)
    ctx["tool_output_chars"] = int(ctx.get("tool_output_chars", 0)) + len(output)
    tool_output_budget_note = None
    if (
        not ctx.get("tool_output_budget_warned")
        and ctx["tool_output_chars"] >= TOOL_OUTPUT_BUDGET_WARNING_CHARS
    ):
        ctx["tool_output_budget_warned"] = True
        tool_output_budget_note = tool_output_budget_reminder(ctx["tool_output_chars"])

    # Append the tool result to the transcript. This must come directly after the
    # assistant message that requested the call -- anything inserted between the
    # two invalidates the request for a strict provider.
    transcript = list(ctx.get("transcript", []))
    transcript.append(
        _from_message(NeutralMessage(role="tool", content=output, tool_call_id=tc.id, name=tc.name))
    )
    # Loop-hygiene guard, at parity with the in-process engine's own
    # `_track_repeat` (engine.py's loop()) -- same shared track_repeat_tool_call,
    # just persisted on run.context instead of a local closure variable, since
    # this runtime drives one tool call per HTTP request with no in-memory state
    # surviving between them.
    ctx["repeat_tracker"], repeat_reminder = track_repeat_tool_call(
        ctx.get("repeat_tracker", {}), tc
    )
    if repeat_reminder is not None:
        transcript.append(_from_message(NeutralMessage(role="user", content=repeat_reminder)))
    if tool_output_budget_note is not None:
        transcript.append(
            _from_message(NeutralMessage(role="user", content=tool_output_budget_note))
        )
    ctx["transcript"] = transcript
    if suspend is not None:
        # The verdict the isolated runtime reads after the container exits, so the
        # executor opens the Clarification and parks the run (same shape as the
        # approval suspend above).
        ctx["isolated_result"] = {"status": suspend, "output": output}
    run.context = ctx

    # Live Log parity with the in-process engine (agent/engine.py's
    # _live_tool_call): the append must land after the whole-column
    # `run.context = ctx` write above (that assignment is a snapshot taken
    # at the top of this request, so an append made before it would just be
    # clobbered) but MUST NOT be split across a separate commit -- `db` is a
    # `tenant_session`, whose RLS-scoping `app.tenant_id` GUC is
    # `SET LOCAL` (transaction-scoped, per db/session.py); a commit in
    # between ends that transaction and drops the GUC, so the raw-SQL
    # append below would run unbound and get RLS-rejected (see project
    # note: "Tenant GUC dies at commit"). An explicit flush -- not a
    # commit -- sends the pending ORM `context = ctx` UPDATE ahead of the
    # raw-SQL append while staying in the same transaction; append_tool_call's
    # own `_adopt()` overwrites this session's in-memory `run.context` with
    # whatever it read back, so if that read happened before this write
    # landed, the flush's changes would be silently lost from the ORM's view
    # (even though the DB row itself would still be correct) -- one commit
    # at the end covers both writes atomically, in the GUC's own transaction.
    await db.flush()
    live_call: dict[str, Any] = {
        "tool": tc.name,
        "arguments": tc.arguments,
        "result": output[:300],
    }
    if dispatched:
        live_call["startedAt"] = started_at.isoformat()
        live_call["durationMs"] = duration_ms
    await append_tool_call(db, run, live_call)
    await db.commit()
    await publish_run_tool_call(run.tenant_id, run_id=run.id, call=live_call)

    if suspend is not None:
        return ToolResult(status=suspend, output=output)
    return ToolResult(status="denied" if decision.effect is Effect.DENY else "ok", output=output)


# --------------------------------------------------------------------- finish


class FinishBody(BaseModel):
    status: str  # done | failed
    output: str = ""


@router.post(
    "/internal/agent/{run_id}/finish",
    dependencies=[Depends(unguarded("run-scoped agent token, verified by the route itself"))],
)
async def finish(
    run_id: uuid.UUID,
    body: FinishBody,
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, str]:
    run = await _run_for_token(run_id, db, principal)
    # The executor (which is driving the container) applies the state transition
    # from the RunResult it builds; here we only record the shell's verdict.
    run.context = {**run.context, "isolated_result": {"status": body.status, "output": body.output}}
    await db.commit()
    return {"status": "recorded"}
