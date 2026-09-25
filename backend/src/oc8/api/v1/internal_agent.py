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
import json
import logging
import uuid
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from oc8 import models as m
from oc8.agent import cache_flow
from oc8.agent.control_tools import (
    CONTROL_TOOL_NAMES,
    FIND_TOOLS,
    execute_control_tool,
    offered_tools,
)
from oc8.agent.engine import _max_steps
from oc8.agent.harness import Harness, resolve_caps
from oc8.agent.harness.calls import call_sig as _call_sig
from oc8.agent.harness.prompts import compaction_instruction
from oc8.agent.harness.retrieval import select_completion_tools
from oc8.agent.harness.step_timing import finish_step, note_model, note_tools, start_step
from oc8.agent.harness.stages.a_compaction import (
    prompt_token_fallback,
    rebuild_transcript,
    should_compact,
)
from oc8.agent.harness.stages.a_masking import mask_observations
from oc8.agent.harness.stages.b_approval import autonomy_of, strip_justification
from oc8.agent.harness.stages.b_authorize import authorize as _authorize
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
from oc8.agent.harness.stages.c_spill import persist_spill
from oc8.agent.harness.procedures import (
    newly_satisfied_lines,
    procedure_haystack,
    satisfied_ids,
)
from oc8.agent.mcp_client import McpSession
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.agent.mcp_requirements import wrap_with_requirements
from oc8.agent.outward import outward_target
from oc8.agent.preamble import build_run_preamble
from oc8.agent.tool_notes import apply_tool_notes
from oc8.agent.tool_semantics import describe_focus, describes_a_record, record_identity
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
from oc8.modelrouter.accumulate import StreamTiming, accumulate_stream
from oc8.modelrouter.keys import resolve_model_base_url
from oc8.modelrouter.sampling import bumped_for_length_retry, resolve_params
from oc8.modelrouter.trim import overflow_tokens
from oc8.modelrouter.types import ImagePart, ModelParams, TextPart, with_prompt_cache_key
from oc8.realtime.emit import note_focus, publish_run_token_delta, publish_run_tool_call
from oc8.runtime.approval_resume import pre_decided_map
from oc8.runtime.registry import BUILTIN_ISOLATED_RUNTIME_REF
from oc8.runtime.run_context import append_tool_call
from oc8.skills.runtime import LoadedSkill, instruction_block, load_assigned_skills
from oc8.storage import s3

router = APIRouter()
logger = logging.getLogger(__name__)

RUN_SCOPE = "run:"


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


def _manifest_guardrail_attributes(
    conn: m.McpConnection | None,
) -> list[dict[str, Any]]:
    if conn is None:
        return []
    cfg = _mcp_params(conn)
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is None:
        return []
    return [
        {
            "key": attribute.key,
            "datatype": attribute.datatype,
            "tools": attribute.tools,
            "extract": attribute.extract,
        }
        for attribute in manifest_conn.guardrail_attributes
    ]


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
    #: True when this agent's resolved caps say the model can cope with
    #: concurrent tool results in one turn (spec §3.5) -- see each
    #: `tool_calls` entry's own `"tier"` key for which calls that covers.
    #: A hint only: the shell decides whether/how to batch, and /tool's own
    #: authorization below runs unconditionally for every call either way.
    parallel_tool_calls: bool = False


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
        # Close any open step timing left by the previous /step→/tool cycle.
        # Without this, `_t0` + step_wall_ms: 0 survive through /finish; the
        # common next-/step finish never runs on this early exit.
        step_timings = ctx.get("stepTimings") or []
        if step_timings and "_t0" in step_timings[-1]:
            finish_step(step_timings[-1])
            run.context = ctx
            await db.commit()
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
            caps=resolve_caps(model_config.params if model_config is not None else None),
            max_steps=_max_steps(agent),
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
        # C3 (a later package) reads this back from run.context so the step
        # stamp uses the SAME resolved timezone as A2's "Now" line, instead of
        # re-resolving org.timezone a second time on every later step.
        ctx["tz"] = preamble.tz
        if conn is not None and not tool_schemas_raw:
            cfg = _mcp_params(conn)
            env = await _mcp_env(conn, db, run.tenant_id)
            command, args = wrap_with_requirements(cfg.get("command", ""), cfg.get("args", []), cfg)
            async with McpSession(command, args, env=env) as s:
                tool_schemas_raw = [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                        "annotations": t.annotations,
                    }
                    for t in apply_tool_notes(s.tools, cfg)
                ]
        ctx["tool_schemas"] = tool_schemas_raw

    mcp_tools = [
        NeutralTool(
            name=t["name"],
            description=t.get("description", ""),
            parameters=t.get("parameters", {"type": "object", "properties": {}}),
            annotations=t.get("annotations"),
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
    # the builtin isolated shell, which has none of its own. run_program rides
    # the same shell gate and additionally requires caps.code_mode.
    offer_write_output_file = (
        not agent.runtime_ref or agent.runtime_ref == BUILTIN_ISOLATED_RUNTIME_REF
    )
    caps = resolve_caps(model_config.params if model_config is not None else None)
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
        offer_run_program=caps.code_mode,
    )

    # See stages/d_todo: a continuation round never crosses a /step HTTP call
    # here -- the shell must never see an intermediate "no tool calls yet"
    # response, since its own loop protocol (isolated_shell.py) has no "keep
    # going anyway" path and would just end the run. So the whole nudge-and-
    # retry cycle happens in this one call via the internal loop below. The
    # round counter is therefore reset per request on purpose (it never has to
    # survive past this one call) -- a known asymmetry with the in-process
    # engine, which counts rounds per run; spec §1.1, package 6.
    harness = Harness.from_run_context(
        ctx,
        caps=caps,
    )
    harness.state.todo_rounds = 0

    skill_tool_names = frozenset(s.tool_name for s in assigned_skills)
    raw_notes = (_mcp_params(conn).get("tool_notes") if conn is not None else None)
    tool_notes = raw_notes if isinstance(raw_notes, dict) else None
    resolved_tools, catalog = select_completion_tools(
        tools,
        control_names=CONTROL_TOOL_NAMES,
        skill_names=skill_tool_names,
        mission=str(ctx.get("task", "")),
        skill_texts=[s.definition.instruction for s in active_skills],
        pinned=list(harness.state.pinned_tools),
        tool_list_may_change=harness.caps.tool_list_may_change,
        mcp_connection=conn.name if conn is not None else None,
        tool_notes=tool_notes,
        find_tools=FIND_TOOLS,
        procedure_texts=_procedure_texts(active_skills),
    )
    harness.state.tool_catalog = catalog
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
        msgs: list[NeutralMessage],
        sampling_params: ModelParams,
        req_id: uuid.UUID,
        *,
        publish: bool = True,
        timing: StreamTiming | None = None,
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
            on_text=_live_token_delta if publish else None,
            timing=timing,
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

    async def _compact() -> None:
        summary_request_id = uuid.uuid4()
        current_messages = _to_messages(transcript)
        summary_result = await _complete(
            [
                *current_messages,
                NeutralMessage(role="user", content=compaction_instruction()),
            ],
            resolved_params,
            summary_request_id,
            publish=False,
        )
        await _record(summary_result, summary_request_id)
        rebuilt = rebuild_transcript(
            current_messages,
            summary=summary_result.text,
            ledger_block=render_ledger_block(harness.state.ledger),
            skill_blocks=[_instruction_for(skill, harness) for skill in active_skills],
        )
        transcript[:] = [_from_message(message) for message in rebuilt]
        harness.state.compactions += 1
        harness.state.last_compacted_step = harness.state.step_no
        harness.state.ledger_sent_hash = ledger_fingerprint(harness.state.ledger)

    while True:
        ledger_hash = ledger_fingerprint(harness.state.ledger)
        if ledger_hash != harness.state.ledger_sent_hash:
            transcript.append(
                _from_message(
                    NeutralMessage(
                        role="user",
                        content=render_ledger_block(harness.state.ledger),
                    )
                )
            )
            harness.state.ledger_sent_hash = ledger_hash

        # Mirror engine.py's per-iteration step stamp: this /step is about to
        # take step ctx["steps"]+1, which is what ctx["steps"] becomes after
        # the completion below.
        harness.state.step_no = int(ctx.get("steps", 0)) + 1
        if should_compact(harness.state, harness.caps):
            await _compact()
        resolved_messages, harness.state.masked = mask_observations(
            _to_messages(transcript),
            step_no=harness.state.step_no,
            ledger=harness.state.ledger,
        )
        request_id = uuid.uuid4()
        # Close any prior open step (tool waits from /tool) before opening the
        # next model-step record. Same key the in-process engine merges via
        # executor.py.
        step_timings = ctx.setdefault("stepTimings", [])
        if step_timings and "_t0" in step_timings[-1]:
            finish_step(step_timings[-1])
        step_rec = start_step(harness.state.step_no)
        step_timings.append(step_rec)
        step_probe = StreamTiming()
        # Department prompt caching, through the SAME helper the in-process engine
        # uses (oc8.agent.cache_flow) -- an isolated deployment must not silently
        # render a settings toggle and a savings figure that do nothing.
        async def _cache_lookup(msgs: list[NeutralMessage]) -> tuple[str | None, Any]:
            return await cache_flow.lookup(
                department=dept,
                tenant_id=run.tenant_id,
                department_id=agent.department_id,
                provider=provider,
                model=model,
                base_url=resolved_base_url,
                messages=msgs,
                tools=resolved_tools,
                params=resolved_params,
                contains_restricted=contains_restricted,
            )

        key, cached_result = await _cache_lookup(resolved_messages)

        overflow_retried = False

        async def _complete_with_overflow_retry(
            sampling_params: ModelParams,
            req_id: uuid.UUID,
        ) -> tuple[Any, uuid.UUID]:
            nonlocal key, resolved_messages, overflow_retried
            stamped = with_prompt_cache_key(sampling_params, str(run.id))
            try:
                return (
                    await _complete(
                        resolved_messages, stamped, req_id, timing=step_probe
                    ),
                    req_id,
                )
            except Exception as exc:
                if overflow_tokens(str(exc)) is None:
                    raise
                if overflow_retried:
                    raise
                overflow_retried = True
                await _compact()
                resolved_messages, harness.state.masked = mask_observations(
                    _to_messages(transcript),
                    step_no=harness.state.step_no,
                    ledger=harness.state.ledger,
                )
                key, _ = await _cache_lookup(resolved_messages)
                retry_request_id = uuid.uuid4()
                return (
                    await _complete(
                        resolved_messages,
                        stamped,
                        retry_request_id,
                        timing=step_probe,
                    ),
                    retry_request_id,
                )

        # Every model turn is metered HERE, because this is where an isolated run's
        # turns happen -- the container holds no keys and never calls a provider. Same
        # record the in-process engine writes after its own turn (§15.3): without it a
        # deployment on OC8_AGENT_ISOLATION=true bills nothing and its budgets never
        # fill, so the runtime's budget gate could never fire.
        if cached_result is not None:
            result = cached_result
            note_model(step_rec, model_wait_ms=0, ttft_ms=None)
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
            result, request_id = await _complete_with_overflow_retry(
                resolved_params,
                request_id,
            )
            await _record(result, request_id)
            if result.stop_reason == "length" and not result.tool_calls and not result.text.strip():
                # See engine.py's identical check: a reasoning-capable model can
                # spend its whole completion budget on hidden reasoning and hit
                # max_tokens before writing anything visible. One retry with
                # double the budget, before this silently reads as the run being
                # finished with nothing actually done.
                retry_request_id = uuid.uuid4()
                result, retry_request_id = await _complete_with_overflow_retry(
                    bumped_for_length_retry(resolved_params),
                    retry_request_id,
                )
                await _record(result, retry_request_id)
            await cache_flow.store_if_matching(key, result, provider=provider, model=model)
            note_model(
                step_rec,
                model_wait_ms=step_probe.model_wait_ms,
                ttft_ms=step_probe.ttft_ms,
            )
            # Read by /tool below, once this step's requested tool calls come back
            # and any of them turns out to have failed for real (see that
            # endpoint's own invalidate call) -- store_if_matching can't know that
            # yet, since it runs before any tool call this completion requested
            # has executed. Same fix as agent/engine.py's step loop, adapted to
            # this endpoint's split /step + /tool request cycle: there is no
            # single in-process loop here to hold the key across both calls, so
            # it travels on the run's own context instead.
            ctx["pending_cache_key"] = key

        harness.state.last_prompt_tokens = (
            result.usage.tokens_in
            if result.usage.tokens_in > 0
            else prompt_token_fallback(resolved_messages)
        )
        transcript.append(
            _from_message(
                NeutralMessage(role="assistant", content=result.text, tool_calls=result.tool_calls)
            )
        )
        ctx["steps"] = int(ctx.get("steps", 0)) + 1

        truncated_empty = (
            result.stop_reason == "length" and not result.tool_calls and not result.text.strip()
        )
        verdict = None
        if not result.tool_calls and not truncated_empty:
            # D1 through the shared Harness: the model tried to finish while
            # its own todo_write checklist still has open items.
            open_todos = [t for t in ctx.get("todos", []) if t.get("status") != "completed"]
            verdict = harness.may_finish(
                open_todos,
                can_continue=int(ctx["steps"]) < _max_steps(agent),
                procedures=_active_procedures(active_skills),
            )
            if not verdict.ok:
                note_tools(step_rec, 0)
                finish_step(step_rec)
                transcript.append(
                    _from_message(NeutralMessage(role="user", content=verdict.reminder or ""))
                )
                continue
        if not result.tool_calls:
            note_tools(step_rec, 0)
            finish_step(step_rec)
        break

    harness.store(ctx)
    ctx["transcript"] = transcript
    run.context = ctx
    await db.commit()

    # Reaching here with no tool call and open todos means the round cap (or
    # the step budget) was hit, not that everything got done -- say so in the
    # output instead of silently looking like a clean finish, same as
    # engine.py's identical check (see todo_continuation_exhausted_note).
    step_text = result.text
    if verdict is not None and verdict.exhausted_note is not None:
        step_text = f"{step_text}\n\n{verdict.exhausted_note}"
    elif truncated_empty:
        # Otherwise this ends as status_override="failed" with an empty
        # text, the shell forwards that empty text to /finish verbatim, and
        # the run closes with no diagnosable reason anywhere -- see
        # engine.py's identical check, whose RunResult.output already carries
        # this same message for the in-process path.
        step_text = "Model exceeded its token budget without producing an answer or tool call."

    # Spec §3.5: a turn's leading run of ALLOW-decision, read-tier,
    # non-control, non-outward tool calls may dispatch concurrently instead
    # of one at a time -- but unlike the in-process engine (engine.py), this
    # endpoint never dispatches anything itself. It only ANNOTATES which
    # calls are safe to batch; isolated_shell.py, on the other side of the
    # HTTP boundary, is the one that actually fires them concurrently.
    # /tool's own `_authorize`+`required_right` (reading real, current, not
    # pre-computed state) stays completely unconditional for every call --
    # this tier is a client-side batching hint, never a bypass.
    caps = resolve_caps(model_config.params if model_config is not None else None)
    scopes = _manifest_scopes(conn)
    skill_tool_names = frozenset(s.tool_name for s in assigned_skills)
    cfg = _mcp_params(conn) if conn is not None else {}
    value_spec = cfg.get("value_spec") if isinstance(cfg.get("value_spec"), dict) else None
    guardrail_attribute_specs = _manifest_guardrail_attributes(conn)
    focus_spec = cfg.get("focus_spec") if isinstance(cfg.get("focus_spec"), dict) else None
    outward_tools = cfg.get("outward_tools") if isinstance(cfg.get("outward_tools"), list) else None
    tool_tiers: dict[str, str] = {}
    if caps.parallel_tool_calls:
        for t in result.tool_calls:
            if t.name in CONTROL_TOOL_NAMES or t.name in skill_tool_names:
                break
            pre_decision = _authorize(
                agent,
                t,
                frame=frame,
                skill_tool_names=skill_tool_names,
                delegation_depth=int(ctx.get("delegation_depth", 0)),
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
                guardrail_attribute_specs=guardrail_attribute_specs,
            )
            if pre_decision.effect is not Effect.ALLOW:
                break
            offered = next((tool for tool in tools if tool.name == t.name), None)
            if (
                classify_tier(
                    t.name,
                    scopes=scopes,
                    config=cfg,
                    annotations=offered.annotations if offered is not None else None,
                )
                != "read"
            ):
                break
            # An outward-declared call (spec B8) breaks the run even though
            # required_right would call it "read" too -- batching it would
            # let two concurrent /tool POSTs both read "not yet delivered"
            # from check_outward before either commits.
            if outward_target(t.name, t.arguments, focus_spec, outward_tools) is not None:
                break
            tool_tiers[t.id] = "read"

    return StepResult(
        done=not result.tool_calls and int(ctx["steps"]) <= _max_steps(agent),
        text=step_text,
        tool_calls=[
            {
                "id": t.id,
                "name": t.name,
                "arguments": t.arguments,
                "tier": tool_tiers.get(t.id, "modify"),
            }
            for t in result.tool_calls
        ],
        # Truncated even after the retry above -- never let the shell read
        # this as "done" (see engine.py's identical check for why).
        status_override="failed" if truncated_empty else None,
        parallel_tool_calls=caps.parallel_tool_calls,
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
    spill: dict[str, str] | None = None  # {filename, content} for the shell mirror


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
    guardrail_attribute_specs = _manifest_guardrail_attributes(conn)
    focus_spec = cfg.get("focus_spec") if isinstance(cfg.get("focus_spec"), dict) else None
    outward_tools = cfg.get("outward_tools") if isinstance(cfg.get("outward_tools"), list) else None
    scopes = _manifest_scopes(conn)
    ctx = dict(run.context)
    harness = Harness.from_run_context(ctx)
    harness.state.step_no = int(ctx.get("steps", 0))

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
        guardrail_attribute_specs=guardrail_attribute_specs,
    )
    stripped, justification = strip_justification(tc.arguments)
    tc.arguments = stripped
    transcript = list(ctx.get("transcript", []))
    for message in reversed(transcript):
        calls = message.get("tool_calls", [])
        matched = next((call for call in calls if call.get("id") == tc.id), None)
        if matched is not None:
            matched["arguments"] = dict(tc.arguments)
            break
    ctx["transcript"] = transcript
    # Honour an operator's earlier decision on this exact call (resume).
    if decision.effect is Effect.REQUIRE_APPROVAL:
        verdict = pre_decided_map(run.context.get("resolved_tool_approvals", [])).get(_call_sig(tc))
        if verdict == "approve":
            decision = Decision(Effect.ALLOW, "operator approved")
        elif verdict == "reject":
            decision = Decision(Effect.DENY, "operator rejected this action")

    tool_schema = next(
        (
            raw
            for raw in ctx.get("tool_schemas", [])
            if isinstance(raw, dict) and raw.get("name") == tc.name
        ),
        None,
    )
    annotations = tool_schema.get("annotations") if tool_schema is not None else None
    typed_annotations = annotations if isinstance(annotations, dict) else None
    idempotent = (
        typed_annotations is not None and typed_annotations.get("idempotentHint") is True
    )
    gate_verdict = None
    tier = None
    if decision.effect is Effect.ALLOW:
        tier = classify_tier(
            tc.name,
            scopes=scopes,
            config=cfg,
            annotations=typed_annotations,
        )
        definition = agent.definition if isinstance(agent.definition, dict) else {}
        raw_b5_grants = definition.get("b5_grants")
        b5_grants = raw_b5_grants if isinstance(raw_b5_grants, list) else []
        gate_verdict = harness.gate(
            tc,
            tier=tier,
            ledger=harness.state.ledger,
            connection=conn.name if conn is not None else "oc8",
            config=cfg,
            autonomy=autonomy_of(definition),
            granted=tier in b5_grants,
            record_label=describe_focus(tc.name, tc.arguments, focus_spec) or "",
            identity=record_identity(tc.name, tc.arguments, focus_spec),
            procedures=_active_procedures(active_skills),
        )
        if gate_verdict.effect == "ask":
            verdict = pre_decided_map(run.context.get("resolved_tool_approvals", [])).get(
                _call_sig(tc)
            )
            if verdict == "approve":
                decision = Decision(Effect.ALLOW, "operator approved")
            elif verdict == "reject":
                decision = Decision(Effect.DENY, "operator rejected this action")
            else:
                decision = Decision(Effect.REQUIRE_APPROVAL, gate_verdict.preview)
        elif gate_verdict.effect == "deny":
            decision = Decision(Effect.DENY, gate_verdict.reason)

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
        step_timings = ctx.setdefault("stepTimings", [])
        if step_timings and "_t0" in step_timings[-1]:
            finish_step(step_timings[-1])
        ar = await raise_approval(
            db,
            tenant_id=run.tenant_id,
            agent_id=agent.id,
            task_id=run.task_id,
            action_type="tool_send",
            title=f"{agent.name} wants to call {tc.name}",
            detail=decision.reason,
            payload={
                "tool": tc.name,
                "arguments": tc.arguments,
                "justification": justification,
                "preview": gate_verdict.preview if gate_verdict is not None else "",
            },
        )
        # Record the suspend verdict so the isolated runtime maps the run to
        # waiting_for_approval after the container exits.
        run.context = {
            **ctx,
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

    definition = agent.definition if isinstance(agent.definition, dict) else {}
    raw_b5_grants = definition.get("b5_grants")
    b5_grants = raw_b5_grants if isinstance(raw_b5_grants, list) else []
    # Ledger outward attribution must follow the tool that actually ran.
    # B4 may replace tc with ask_user / request_decision; keep the gated name.
    gated_tool_name = tc.name
    if (
        decision.effect is Effect.ALLOW
        and gate_verdict is not None
        and should_clarify(
            tier=gate_verdict.tier,
            autonomy=autonomy_of(definition),
            granted=gate_verdict.tier in b5_grants,
            clarify_enabled=definition.get("clarify_before_irreversible") is not False,
            already_done=harness.state.clarification_done,
        )
    ):
        clarify_tier = gate_verdict.tier
        clarify_tool = tc.name
        transcript_for_ctx = list(ctx.get("transcript", []))
        run_context = next(
            (
                entry.get("content", "")
                for entry in transcript_for_ctx
                if entry.get("role") == "user"
                and isinstance(entry.get("content"), str)
                and entry["content"].startswith("# Run context")
            ),
            "",
        )
        call_json = json.dumps(
            {"name": tc.name, "arguments": strip_justification(tc.arguments)[0]},
            sort_keys=True,
        )
        clarify_msgs = clarification_prompt(
            task_text=str(ctx.get("task", "")),
            run_context=run_context,
            ledger_block=render_ledger_block(harness.state.ledger),
            call_json=call_json,
        )
        settings = get_settings()
        model_config = (
            await db.get(m.ModelConfig, agent.model_config_id) if agent.model_config_id else None
        )
        if model_config is not None:
            provider, model = model_config.provider, model_config.model
        else:
            provider = (agent.presentation or {}).get("provider", settings.default_model_provider)
            model = settings.default_model
        contains_restricted = bool(ctx.get("contains_restricted", False))
        resolved_params = resolve_params(model_config, agent=agent)

        async def _complete(
            msgs: list[NeutralMessage],
            sampling_params: ModelParams,
            req_id: uuid.UUID,
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
                    tools=[],
                    params=sampling_params,
                    request_id=req_id,
                    contains_restricted=contains_restricted,
                ),
                on_text=None,
            )

        reply_text = "NONE"
        try:
            clarify_req_id = uuid.uuid4()
            clarify_result = await _complete(
                clarify_msgs,
                replace(resolved_params, temperature=0.0),
                clarify_req_id,
            )
            await record_usage(
                db,
                tenant_id=run.tenant_id,
                request_id=clarify_req_id,
                model=clarify_result.model,
                provider=clarify_result.provider,
                tokens_in=clarify_result.usage.tokens_in,
                tokens_out=clarify_result.usage.tokens_out,
                agent_id=agent.id,
                department_id=agent.department_id,
            )
            reply_text = clarify_result.text or ""
        except Exception:
            logger.warning(
                "clarification checkpoint failed for tool=%s",
                clarify_tool,
                exc_info=True,
            )
        tc = apply_clarification(
            tc, reply_text, chat=run.source == "chat", state=harness.state
        )
        facts = parse_clarification(reply_text)
        logger.info(
            "clarification checkpoint tool=%s tier=%s %s",
            clarify_tool,
            clarify_tier,
            facts if facts is not None else "NONE",
        )
        harness.store(ctx)

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
    tool_error: ToolError | None = None
    source = conn.name if conn is not None else "oc8"
    writes = required_right(tc.name, scopes) != "read"
    access_identity = record_identity(tc.name, tc.arguments, focus_spec)
    identity = access_identity if writes else None
    record_label = (
        describe_focus(tc.name, tc.arguments, focus_spec)
        or (
            f"{access_identity[0]} {access_identity[1]}"
            if access_identity is not None
            else ""
        )
    )
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
            harness_state=harness.state,
            active_procedure_skills=[s for s in active_skills if s.definition.steps],
        )
        if task is not None
        else None
    )

    if control is not None:
        output = control.output
        source = "oc8"
        if control.output.startswith("ERROR:"):
            tool_error = ToolError(
                kind="control",
                message=control.output.removeprefix("ERROR: ").strip(),
            )
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
        reason = decision.reason or "denied"
        output = f"ERROR: {reason}"
        source = "oc8"
        tool_error = ToolError(kind="deny", message=reason)
        dispatched = False
    elif conn is None:
        output = "ERROR: no tool server available"
        tool_error = ToolError(kind="deny", message="no tool server available")
        dispatched = False
    elif (
        blast_refusal := await check_blast_radius(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
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
        dispatched = False
    elif (
        claim_refusal := await claim_write(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
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
        dispatched = False
    elif (
        outward := await check_outward(
            db,
            tenant_id=run.tenant_id,
            task_id=run.task_id,
            tc=tc,
            focus_spec=focus_spec,
            outward_tools=outward_tools,
        )
    ).refusal is not None:
        # Checked before the call, not after: the point is that the recipient is
        # not reached twice, and a check that ran afterwards could only report it.
        output = outward.refusal
        source = "oc8"
        tool_error = ToolError(
            kind="deny",
            message=outward.refusal.removeprefix("ERROR: ").strip(),
        )
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
        replay = await replay_for(
            db,
            tenant_id=run.tenant_id,
            task_id=run.task_id,
            tc=tc,
            writes=writes,
            idempotent=idempotent,
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
                tool_error = classify_exception(
                    exc,
                    duration_s=(dt.datetime.now(dt.UTC) - started_at).total_seconds(),
                )
                output = f"ERROR: {exc}"
            # Only a successful side effect is worth recording. Recording a failure
            # would answer a legitimate retry with the old error forever.
            await remember_outward(
                db,
                tenant_id=run.tenant_id,
                task_id=run.task_id,
                target=outward.target,
                output=output,
            )
            await record_for(
                db,
                tenant_id=run.tenant_id,
                task_id=run.task_id,
                tc=tc,
                writes=writes,
                output=output,
                idempotent=idempotent,
            )

    succeeded = tool_error is None and not output.startswith("ERROR:")
    procs = _active_procedures(active_skills)
    before_sat = _satisfied_map(procs, harness)
    if succeeded:
        record_tool(harness.state.ledger, tc.name)
        if access_identity is not None and conn is not None:
            note_access(
                harness.state.ledger,
                connection=conn.name,
                kind=access_identity[0],
                id=access_identity[1],
                label=record_label,
                step_no=harness.state.step_no,
                wrote=writes,
                tool=tc.name,
                exempt_unverified=(tier == "outward" and tc.name == gated_tool_name),
            )
        if tier == "outward" and tc.name == gated_tool_name:
            record_outward(
                harness.state.ledger,
                connection=conn.name if conn is not None else "oc8",
                tool=tc.name,
                target=str(tc.arguments.get("target") or tc.arguments.get("to") or ""),
                step=harness.state.step_no,
            )
        if tc.name == "write_output_file":
            record_file(harness.state.ledger, str(tc.arguments.get("filename", "")))
        if tc.name in {"request_decision", "ask_user"}:
            record_decision(
                harness.state.ledger,
                tool=tc.name,
                question=str(tc.arguments.get("question", "")),
                step=harness.state.step_no,
            )
    after_sat = _satisfied_map(procs, harness)
    flip_lines = newly_satisfied_lines(
        skills=procs, before=before_sat, after=after_sat
    )

    # Stopped HERE, the moment the call itself returned -- not at the append
    # site far below, which is separated from it by the transcript rewrite and
    # a `db.flush()` whose time is this request's, not the tool's.
    duration_ms = int((dt.datetime.now(dt.UTC) - started_at).total_seconds() * 1000)

    # Accumulate onto the open step record from /step; that record stays open
    # until the next /step finishes it (tool wait = sum of /tool calls before
    # the next model step). A suspend ends the step without another /step.
    step_timings = ctx.setdefault("stepTimings", [])
    if dispatched and step_timings and "_t0" in step_timings[-1]:
        open_rec = step_timings[-1]
        note_tools(open_rec, int(open_rec.get("tool_wait_ms", 0)) + duration_ms)
        if suspend is not None:
            finish_step(open_rec)

    # This tool call belongs to the completion /step just cached (see its own
    # ctx["pending_cache_key"] comment) -- a real failure here means that
    # completion's first-try guess was wrong, so it must not replay verbatim
    # on every identical future trigger for the rest of the cache TTL.
    if output.startswith("ERROR:") and ctx.get("pending_cache_key") is not None:
        await cache_flow.invalidate(ctx["pending_cache_key"])

    # C2 + C1 + C3 + C5 through the shared Harness, at parity with the
    # in-process engine's loop -- same error envelope, spill preview, stamp,
    # cumulative counter, and repeat tracker, just persisted on run.context
    # instead of a local closure
    # variable, since this runtime drives one tool call per HTTP request with
    # no in-memory state surviving between them. ctx["steps"] is the same
    # counter /step increments (see its own ctx["steps"] = ... line above).
    shaped = harness.shape(
        tc,
        output,
        max_steps=_max_steps(agent),
        tz=str(ctx.get("tz", "UTC")),
        source=source,
        error=tool_error,
    )
    output = shaped.output
    shaped.reminders.extend(flip_lines)
    if succeeded and shaped.spill is not None:
        record_file(harness.state.ledger, shaped.spill.filename)
    if shaped.spill is not None:
        try:
            await persist_spill(db, tenant_id=run.tenant_id, run_id=run.id, spill=shaped.spill)
        except Exception:
            logger.exception("run %s: spill persistence escaped safe boundary", run.id)
    spill_payload = (
        {"filename": shaped.spill.filename, "content": shaped.spill.content}
        if shaped.spill is not None
        else None
    )

    # Append the tool result to the transcript. This must come directly after the
    # assistant message that requested the call -- anything inserted between the
    # two invalidates the request for a strict provider.
    transcript.append(
        _from_message(NeutralMessage(role="tool", content=output, tool_call_id=tc.id, name=tc.name))
    )
    for reminder in shaped.reminders:
        transcript.append(_from_message(NeutralMessage(role="user", content=reminder)))
    harness.store(ctx)
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
        return ToolResult(status=suspend, output=output, spill=spill_payload)
    return ToolResult(
        status="denied" if decision.effect is Effect.DENY else "ok",
        output=output,
        spill=spill_payload,
    )


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
