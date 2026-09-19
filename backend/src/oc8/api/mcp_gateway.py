"""oc8 as the tool gateway: an MCP server agent containers talk to (§8.7 R2).

The container is configured with exactly one MCP server -- us -- and never learns
the real one exists. Credentials are resolved control-plane-side, every call goes
through the PEP (§5.3), and a call over the department's threshold parks the run
for a human. That last part is the reason this exists: the >3000 € approval is
enforceable only because the call passes through here.

Same principle as the LLM gateway: speak the protocol the harness already speaks.
A Claude-Agent-SDK-style runtime knows how to use a remote MCP server, so that is
the seam rather than a bespoke REST shape it would have to learn.

JSON-RPC over a single POST, which is what MCP's streamable-HTTP transport is. A
plain JSON response is a valid reply; SSE is only needed for server-initiated
messages, and this server initiates none.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select, text

from oc8 import models as m
from oc8.agent import mcp_pool
from oc8.agent.blast_radius import alarm as blast_alarm
from oc8.agent.blast_radius import records_per_run, records_touched
from oc8.agent.blast_radius import refusal as blast_refusal
from oc8.agent.claims import claim_record, held_by_run
from oc8.agent.claims import refusal as claim_refusal
from oc8.agent.control_tools import (
    ASK_USER,
    CONTROL_TOOL_NAMES,
    DELEGATE_TASK,
    MEMORY_WRITE,
    RENDER_COMPONENT,
    REQUEST_DECISION,
    SEARCH_KNOWLEDGE,
    SEARCH_MEMORY,
    execute_control_tool,
)
from oc8.agent.engine import _authorize, _call_sig
from oc8.agent.mcp_client import resolve_auth_header
from oc8.agent.mcp_env import has_oauth_ref, resolve_mcp_env
from oc8.agent.mcp_requirements import wrap_with_requirements
from oc8.agent.outward import (
    REFUSAL,
    already_delivered,
    outward_target,
    remember_delivery,
)
from oc8.agent.provenance import fence
from oc8.agent.tool_idempotency import (
    record_invocation,
    replayed_result,
    warn_unclassified_connection,
)
from oc8.agent.tool_routing import Route, build_routes, resolve
from oc8.agent.tool_semantics import (
    describe_focus,
    describes_a_record,
    focus_ref_id,
    record_identity,
    record_title,
)
from oc8.api.deps import CurrentPrincipal, DbSession
from oc8.approvals import raise_approval
from oc8.audit import append_event
from oc8.authz.pdp import (
    Decision,
    Effect,
    effective_tool_policies,
    required_right,
)
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.memory.policy import authorize_memory_write
from oc8.realtime.emit import note_focus, record_activity
from oc8.runtime.approval_resume import pre_decided_map
from oc8.runtime.run_context import merge_context
from oc8.skills.runtime import instruction_block, load_assigned_skills, skill_tool_schemas

logger = logging.getLogger(__name__)
router = APIRouter()

RUN_SCOPE = "run:"
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "oc8-tool-gateway"

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


def _result(rpc_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": payload}


def _error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """An MCP tool result.

    A policy decision is reported HERE, as a tool result the model can read, never
    as a JSON-RPC error: a transport error looks like a broken server, and a harness
    responds by retrying or aborting instead of letting the model adapt.
    """
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


async def _caller(
    db: DbSession, principal: CurrentPrincipal
) -> tuple[m.AgentRun, m.Agent, m.Department | None, list[m.McpConnection]]:
    """The run, agent, department and reachable connections behind this request.

    Identity comes from the token, never the payload: only a `kind=agent` token
    scoped to a run may drive tools. An operator token must not become a way to act
    on a tenant's systems while bypassing the operator API's own checks.
    """
    if principal.kind != "agent":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tool gateway requires an agent token")
    run_id: uuid.UUID | None = None
    for scope in principal.scopes or []:
        if scope.startswith(RUN_SCOPE):
            try:
                run_id = uuid.UUID(scope[len(RUN_SCOPE) :])
            except ValueError:
                continue
            break
    if run_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent token is not scoped to a run")
    run = await db.get(m.AgentRun, run_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    agent = await db.get(m.Agent, run.agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    dept = await db.get(m.Department, agent.department_id)
    return run, agent, dept, await _connections(db, run, dept)


async def _connections(
    db: DbSession, run: m.AgentRun, dept: m.Department | None
) -> list[m.McpConnection]:
    """Every system this run may reach, not merely the first one.

    A pinned `mcp_connection_id` still wins: a delegation or an explicit
    "run against THIS connection" means exactly that. Everything else gets the
    department's connected systems -- all of them. The old `.limit(1)` here is
    why an agent set up with two systems silently saw one.
    """
    mcp_id = run.context.get("mcp_connection_id")
    if mcp_id:
        pinned = await db.get(m.McpConnection, uuid.UUID(str(mcp_id)))
        return [pinned] if pinned is not None else []
    if dept is None:
        return []
    found = (
        (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.department_id == dept.id,
                    m.McpConnection.connected.is_(True),
                )
                .order_by(m.McpConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    # Names are how a call is routed, so two connections cannot share one. The
    # older wins and the younger is dropped rather than silently swallowing the
    # other's tools -- a wrong-system write is worse than a missing tool.
    seen: dict[str, m.McpConnection] = {}
    for conn in found:
        if conn.name in seen:
            logger.warning(
                "department %s has two connections named %r; ignoring %s",
                dept.id,
                conn.name,
                conn.id,
            )
            continue
        seen[conn.name] = conn
    return list(seen.values())


async def _remember_routes(db: DbSession, run: m.AgentRun, routes: dict[str, Route]) -> None:
    """Keep the routing table on the run, so a CALL need not rediscover it.

    Discovery costs a subprocess per connection. Paying that on every tool call
    -- on top of the subprocess the call itself needs -- would double the cost
    of every action for the sake of a mapping that cannot change mid-run.
    """
    await merge_context(
        db,
        run,
        {"tool_routes": {name: [r.connection, r.tool] for name, r in routes.items()}},
    )


async def _routes(
    db: DbSession,
    run: m.AgentRun,
    agent: m.Agent,
    dept: m.Department | None,
    conns: list[m.McpConnection],
) -> dict[str, Route]:
    """The run's routing table, discovering it only if nothing recorded it yet.

    A harness lists tools before calling them, so the cached path is the normal
    one. The fallback exists because "the model called a tool we never
    advertised" must fail as an unknown tool, not as a missing table.
    """
    cached = run.context.get("tool_routes")
    if isinstance(cached, dict) and cached:
        return {
            str(name): Route(str(pair[0]), str(pair[1]))
            for name, pair in cached.items()
            if isinstance(pair, list) and len(pair) == 2
        }
    await _list_tools(db, run=run, agent=agent, dept=dept, conns=conns)
    cached = run.context.get("tool_routes")
    return (
        {
            str(name): Route(str(pair[0]), str(pair[1]))
            for name, pair in cached.items()
            if isinstance(pair, list) and len(pair) == 2
        }
        if isinstance(cached, dict)
        else {}
    )


def _cfg(conn: m.McpConnection | None) -> dict[str, Any]:
    return conn.config if conn is not None and isinstance(conn.config, dict) else {}


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
    cfg = _cfg(conn)
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is not None and isinstance(manifest_conn.scopes, dict):
        return manifest_conn.scopes
    return conn.scopes if isinstance(conn.scopes, dict) else None


def _manifest_guardrail_attributes(conn: m.McpConnection | None) -> list[dict[str, Any]]:
    """This connection's declared `GuardrailAttribute`s (see `capas/manifest.py`),
    resolved the same way `_manifest_scopes` resolves scopes -- a live tool
    call needs the plugin's own attribute catalog to know which named,
    typed values it may extract for a `Condition` (`authz/pdp.py`) to
    evaluate. Empty for a connection with no matching manifest."""
    if conn is None:
        return []
    cfg = _cfg(conn)
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is None:
        return []
    return [
        {"key": a.key, "datatype": a.datatype, "tools": a.tools, "extract": a.extract}
        for a in manifest_conn.guardrail_attributes
    ]


async def _env(conn: m.McpConnection, db: DbSession, tenant_id: uuid.UUID) -> dict[str, str]:
    return await resolve_mcp_env(db, tenant_id=tenant_id, cfg=_cfg(conn), connection_name=conn.name)


# ------------------------------------------------------------------ tools/list


async def _list_tools(
    db: DbSession,
    *,
    run: m.AgentRun,
    agent: m.Agent,
    dept: m.Department | None,
    conns: list[m.McpConnection],
) -> list[dict[str, Any]]:
    """The tools this agent may actually use, in MCP shape, across every system.

    Filtered by the frame rather than advertised wholesale: offering a tool whose
    every call would be denied only invites the model to waste turns on it -- the
    same reasoning as withholding delegate_task from a non-lead. The frame is
    read PER CONNECTION, so a department can grant one system and withhold
    another.
    """
    frame = dept.frame if dept is not None else {}
    policies = effective_tool_policies(frame, agent.narrowing or {})

    allowed: dict[str, list[Any]] = {}
    for conn in conns:
        policy = policies.get(conn.name)
        if policy is None or not policy.enabled:
            continue
        scopes = _manifest_scopes(conn)
        cfg = _cfg(conn)
        try:
            # INSIDE the guard, not before it: resolving the environment can now
            # fail on its own (an OAuth-backed connection mints a token here, so
            # an Azure blip or a revoked consent raises), and outside the try
            # that failure would take the whole list down -- which is exactly the
            # incident the except below was written for.
            env = await _env(conn, db, run.tenant_id)
            headers = resolve_auth_header(cfg, env)
            # Wrapped HERE, not in mcp_pool: the pool has no cfg, and this is
            # the launch path every packaged/containerized runtime uses. Without
            # it a connection's `requirements` overlay reaches the in-process
            # engine and the Test-connection button but not the actual run --
            # green test, ModuleNotFoundError agent. The pool keys its live-session
            # cache on connection_id alone (not command/args), so wrapping here
            # changes only what gets launched, never the cache key.
            command, args = wrap_with_requirements(cfg.get("command", ""), cfg.get("args", []), cfg)
            discovered = await mcp_pool.tools(
                conn.id,
                command=command,
                args=args,
                env=env,
                transport=conn.transport,
                server_url=conn.server_url,
                headers=headers,
                http_tools=list(cfg.get("http_tools", [])),
                reusable=not has_oauth_ref(cfg),
            )
        except Exception:
            # One unreachable system must not take the whole list with it. It did:
            # the exception escaped tools/list, the bridge registered with ZERO
            # tools, and the agent lost its MEMORY, its KNOWLEDGE and every SKILL
            # -- none of which have anything to do with the system that was down.
            # Observed live 2026-07-30: a stdio server that exits at startup
            # ("MCPError: Connection closed") left an agent answering "no tool is
            # available to me", which reads like a configuration mistake and is
            # not one.
            #
            # Skipped rather than reported as a tool: an agent cannot repair a
            # connection, and a fake tool that only ever errors would spend its
            # steps. The operator learns from this log line and the connection's
            # own health, which is where an unreachable system belongs.
            logger.warning(
                "connection %s (%s) offers no tools right now; the rest stay available",
                conn.name,
                conn.id,
                exc_info=True,
            )
            continue
        allowed[conn.name] = [
            t
            for t in discovered
            if policy.offers(t.name) and policy.has_right(required_right(t.name, scopes))
        ]

    # Built from what the agent may ACTUALLY use: a tool the frame withholds on
    # one system must not force the other system's identical tool to be
    # advertised qualified, when from the model's side there is no ambiguity.
    routes = build_routes({name: [t.name for t in tools] for name, tools in allowed.items()})
    await _remember_routes(db, run, routes)
    by_route = {(r.connection, r.tool): advertised for advertised, r in routes.items()}

    out: list[dict[str, Any]] = []
    for conn_name, tools in allowed.items():
        for tool in tools:
            out.append(
                {
                    "name": by_route[(conn_name, tool.name)],
                    "description": tool.description,
                    # MCP calls it inputSchema; the neutral type calls it parameters.
                    "inputSchema": tool.parameters or {"type": "object", "properties": {}},
                }
            )

    # Core tools a container-run agent may use. REQUEST_DECISION, SEARCH_KNOWLEDGE,
    # SEARCH_MEMORY and MEMORY_WRITE record and return -- they never suspend the
    # caller -- so they were always safe to advertise. ASK_USER used to be refused
    # here for the same reason the lifecycle tools still are (see the
    # CONTROL_TOOL_NAMES catch-all in _call_tool): MCP had no vocabulary for
    # suspending a run. It does now -- _call_tool's own ASK_USER branch gives it
    # real park-and-resume semantics (WAITING_FOR_INPUT, same as an approval), so
    # it is advertised like any other core tool. Withholding any of these would
    # mean the only agents able to hand a decision to a human are the ones NOT
    # running in a container -- that is, not the ones doing the work.
    core = [
        REQUEST_DECISION,
        SEARCH_KNOWLEDGE,
        SEARCH_MEMORY,
        MEMORY_WRITE,
        RENDER_COMPONENT,
    ]
    # Withheld from the tenant Assistant specifically, mirroring
    # control_tools.offered_tools' own reasoning: a park only WORKS if
    # something can answer it, and Telegram free text -- one of the
    # Assistant's own doors -- has no reply-to-a-clarification path at all.
    if not agent.is_tenant_assistant:
        core.append(ASK_USER)
    if agent.is_team_lead:
        # Same argument as REQUEST_DECISION above, one step further. delegate_task
        # was withheld here as a lifecycle tool, but it is not one: it creates a
        # run for SOMEBODY ELSE and returns a sentence -- it never suspends the
        # caller (unlike ask_user, which now parks the caller itself, above).
        # Withholding it meant a team lead could only delegate when NOT running
        # in a container, i.e. never in the mode this system actually runs.
        # Observed live 2026-07-29: an accepted handoff put an order on a lead,
        # and she "delegated" by writing the sub-tasks in prose, because the
        # tool she was told to call was not on her list. Withheld from a
        # non-lead for the reason above: every call would be denied.
        core.append(DELEGATE_TASK)
    for core_tool in core:
        out.append(
            {
                "name": core_tool.name,
                "description": core_tool.description,
                "inputSchema": core_tool.parameters,
            }
        )

    # The agent's own skills, on the same footing as the connection's tools.
    # Skills activate ON DEMAND -- the preamble hands out a catalogue and tells
    # the agent to call the named tool to load the procedure -- so a skill that
    # is never advertised here can never be loaded by a container-run agent, and
    # the rules inside it never apply to anything. Observed live: the helpdesk
    # agent promised a customer a refund its own skill forbids, with
    # `active_skill_ids` empty because there had been nothing to call.
    for schema in skill_tool_schemas(
        await load_assigned_skills(db, agent=agent, tenant_id=run.tenant_id)
    ):
        out.append(
            {
                "name": schema.name,
                "description": schema.description,
                "inputSchema": schema.parameters,
            }
        )
    return out


# ------------------------------------------------------------------ tools/call


async def _rebind_tenant(db: DbSession, tenant_id: uuid.UUID) -> None:
    """Re-pin the RLS GUC after a commit inside a request.

    `tenant_session` pins the tenant transaction-locally (`is_local=true`), so a
    commit in the middle of a request drops it and every later statement runs
    unbound -- where RLS fails closed and silently sees nothing. Same trap as the
    mid-run commit in runtime/isolated.py.
    """
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
    )


async def _stop_for_blast_radius(
    db: DbSession,
    *,
    run: m.AgentRun,
    agent: m.Agent,
    entity: str,
    ref: str,
    touched: int,
    limit: int,
) -> dict[str, Any]:
    """Stop a run that is reaching further than a run should, and tell a human.

    Parked rather than failed, using the same marker the approval gate uses: the
    adapter tears the container down within seconds, and a colleague who finds
    the work legitimate can let it continue. A hard failure would make a false
    positive expensive and a real attack no less visible.
    """
    ar = await raise_approval(
        db,
        tenant_id=run.tenant_id,
        agent_id=agent.id,
        task_id=run.task_id,
        action_type="blast_radius",
        title=f"{agent.name} changed {touched} records in one run",
        detail=blast_alarm(agent.name, touched, limit, entity, ref),
        payload={"entity": entity, "record": ref, "touched": touched, "limit": limit},
    )
    await merge_context(
        db,
        run,
        {
            "isolated_result": {
                "status": "waiting_for_approval",
                "output": blast_alarm(agent.name, touched, limit, entity, ref),
            }
        },
    )
    await db.commit()
    await _rebind_tenant(db, run.tenant_id)
    from oc8.realtime.bus import get_event_bus

    await get_event_bus().publish_event(
        run.tenant_id,
        "approval.created",
        {
            "approval_id": str(ar.id),
            "action_type": ar.action_type,
            "status": ar.status,
            # title/detail travel in the envelope so the push hook never has to
            # re-read this row from a fresh, uncommitted-blind session (see
            # EventBus._push_payload).
            "title": ar.title,
            "detail": ar.detail,
        },
        source=f"oc8/approval/{ar.id}",
    )
    return _tool_result(blast_refusal(touched, limit), is_error=True)


async def _call_tool(
    db: DbSession,
    *,
    run: m.AgentRun,
    agent: m.Agent,
    dept: m.Department | None,
    conns: list[m.McpConnection],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    frame = dept.frame if dept is not None else {}
    # Which system this call belongs to, and what it is called THERE. Every
    # seam below -- the right classification, the value, the record, whether it
    # reaches a person -- belongs to that connection and to no other, which is
    # exactly what a single per-run connection could not express.
    conn: m.McpConnection | None = None
    if conns:
        route = resolve(name, await _routes(db, run, agent, dept, conns))
        if route is not None:
            conn = next((c for c in conns if c.name == route.connection), None)
            name = route.tool
        else:
            conn = None if len(conns) > 1 else conns[0]
    cfg = _cfg(conn)
    value_spec = cfg.get("value_spec") if isinstance(cfg.get("value_spec"), dict) else None
    focus_spec = cfg.get("focus_spec") if isinstance(cfg.get("focus_spec"), dict) else None
    outward_tools = cfg.get("outward_tools") if isinstance(cfg.get("outward_tools"), list) else None
    scopes = _manifest_scopes(conn)
    if conn is not None and scopes is None:
        warn_unclassified_connection(conn.id, conn.name)

    assigned = await load_assigned_skills(db, agent=agent, tenant_id=run.tenant_id)
    skill_tool_names = frozenset(s.tool_name for s in assigned)
    active_ids = {str(s) for s in run.context.get("active_skill_ids", [])}
    active = [s for s in assigned if str(s.skill_version_id) in active_ids]

    if name in skill_tool_names:
        skill = next(s for s in assigned if s.tool_name == name)
        if skill in active:
            return _tool_result(f"Skill '{skill.name}' is already active.")
        await merge_context(
            db,
            run,
            {"active_skill_ids": sorted(active_ids | {str(skill.skill_version_id)})},
        )
        await append_event(
            db,
            tenant_id=run.tenant_id,
            actor_type="agent",
            actor_id=agent.id,
            category="tool_action",
            action="skill.invoked",
            resource={
                "skill_id": str(skill.skill_id),
                "skill_version_id": str(skill.skill_version_id),
                "agent_id": str(agent.id),
            },
        )
        await record_activity(
            db,
            tenant_id=run.tenant_id,
            agent_id=agent.id,
            status="info",
            message=f"Skill activated: {skill.name}",
        )
        # The procedure IS the tool result -- same reasoning as the in-process
        # path in control_tools.py: a mid-conversation system message is rejected
        # outright by strict backends, so there must be no extra message to place.
        return _tool_result(
            f"Skill '{skill.name}' activated. Follow this procedure:\n\n{instruction_block(skill)}"
        )

    if name == ASK_USER.name:
        question = str(arguments.get("question", "")).strip()
        if not question:
            return _tool_result("ERROR: ask_user requires a non-empty question", is_error=True)
        # ONLY the park marker -- structurally identical to the
        # REQUIRE_APPROVAL branch below (write the marker, commit, rebind,
        # publish, return a readable "parked, do not retry" error) and, like
        # it, deliberately performing NO state transition of its own.
        #
        # This used to also call request_clarification() here, which itself
        # does the RUNNING -> WAITING_FOR_INPUT transition. That double-books
        # the park: the adapter sees this marker, returns
        # RunResult(status="waiting_for_input"), and executor.py's generic
        # handling for that status calls request_clarification() a SECOND
        # time on a run already IN waiting_for_input -- a transition
        # runtime/states.py does not allow (WAITING_FOR_INPUT's legal
        # successors are QUEUED/RUNNING/FAILED), so it raises and rolls the
        # executor's whole transaction back.
        #
        # executor.py's call is the ONE place a Clarification row and that
        # transition are created, and every other runtime already depends on
        # it: nanoclaw recognises its harness's native ask_user_question
        # client-side and never touches /mcp at all, and the in-process
        # engine's ASK_USER control tool (control_tools.py) likewise only
        # returns ControlOutcome(suspend="waiting_for_input") without
        # creating anything itself. This branch now behaves the same way.
        # Merged in SQL, not read-modify-written in Python: the harness plugin
        # commits its own session id into this same column while this request is
        # in flight (see cli_harness/session_state.py), and a whole-object write
        # built from a snapshot taken at the top of the request would silently
        # revert it -- the next resume leg would then resume the wrong session
        # or none at all.
        await merge_context(
            db,
            run,
            {"isolated_result": {"status": "waiting_for_input", "output": question}},
        )
        await db.commit()
        await _rebind_tenant(db, run.tenant_id)
        return _tool_result(
            "This question needs the operator. The run is parked; it continues "
            "once they answer. Do not retry and do not work around it.",
            is_error=True,
        )

    if name in (
        REQUEST_DECISION.name,
        SEARCH_KNOWLEDGE.name,
        SEARCH_MEMORY.name,
        MEMORY_WRITE.name,
        DELEGATE_TASK.name,
        RENDER_COMPONENT.name,
    ):
        if run.task_id is None:
            return _tool_result(
                "ERROR: this run has no task to attach a decision to", is_error=True
            )
        task = await db.get(m.Task, run.task_id)
        if task is None:
            return _tool_result("ERROR: task not found", is_error=True)
        from oc8.modelrouter import ToolCall as _ToolCall

        # Reading and asking change nothing outside oc8, so they are allowed
        # outright -- knowledge and memory carry their own grant checks inside.
        # WRITING memory does not: the tier policy is the whole protection there
        # (§10.1), so it is evaluated properly rather than waved through, and a
        # company-tier write comes back as REQUIRE_APPROVAL.
        core_tc = _ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
        if name == MEMORY_WRITE.name:
            core_decision = authorize_memory_write(
                frame, agent.narrowing or {}, str(arguments.get("tier", ""))
            )
        elif name == DELEGATE_TASK.name:
            # Not waved through: _authorize is where "not yourself", "a real
            # agent id" and the depth limit live, and the limit is the only thing
            # standing between a lead and a delegation loop.
            core_decision = _authorize(
                agent,
                core_tc,
                frame=frame,
                skill_tool_names=skill_tool_names,
                delegation_depth=int(run.context.get("delegation_depth", 0)),
                skill_thresholds=(),
                tool_policies=effective_tool_policies(frame, agent.narrowing or {}),
                # A core tool belongs to no connection, so it has neither scopes
                # nor a connection key -- the checks that matter for it (self,
                # real id, depth) are inside _authorize.
                tool_scopes=None,
                connection_key=None,
            )
        else:
            core_decision = Decision(Effect.ALLOW)
        outcome = await execute_control_tool(
            db,
            tenant_id=run.tenant_id,
            agent=agent,
            task=task,
            tc=core_tc,
            decision=core_decision,
            assigned_skills=assigned,
            active_skills=active,
            mcp_conn=conn,
            originating_operator=run.context.get("originating_operator"),
            run_id=run.id,
        )
        assert outcome is not None
        if outcome.pending_run is not None:
            # The sub-run exists but nothing is listening for it yet: publishing
            # from here would race the commit, and an entry on the stream whose
            # row is not yet visible is a run a worker picks up and cannot find.
            # So it rides out on the run's context and the runtime hands it to
            # the executor, which publishes after committing -- the same route
            # the other isolated runtime already uses.
            await merge_context(
                db,
                run,
                {
                    "pending_runs": [
                        *run.context.get("pending_runs", []),
                        str(outcome.pending_run),
                    ]
                },
            )
        if outcome.rendered_component is not None:
            # Unlike pending_run above, this event carries its whole payload
            # inline (run_id + props) -- no consumer needs to look up a row
            # that isn't committed yet, so publishing before the request's
            # terminal commit is safe here.
            from oc8.realtime.bus import get_event_bus

            await get_event_bus().publish_event(
                run.tenant_id,
                "run.component_rendered",
                {"run_id": str(run.id), **outcome.rendered_component},
                source=f"oc8/run/{run.id}",
            )
        return _tool_result(outcome.output, is_error=outcome.output.startswith("ERROR"))

    if name in CONTROL_TOOL_NAMES:
        # Every member of CONTROL_TOOL_NAMES is intercepted by name above --
        # REQUEST_DECISION/SEARCH_KNOWLEDGE/SEARCH_MEMORY/MEMORY_WRITE/
        # DELEGATE_TASK by the block just before this one, ASK_USER by its own
        # branch further up (it now has real MCP semantics: park + resume,
        # same as an approval) -- so nothing reaches this branch today. It
        # stays as a backstop: a control tool added to CONTROL_TOOL_SCHEMAS
        # later without its own _call_tool branch lands here with a readable
        # refusal instead of falling through to the generic tool-routing
        # pipeline below, which would misread it as an ordinary connection
        # tool.
        return _tool_result(
            f"ERROR: '{name}' is not available through the tool gateway", is_error=True
        )

    from oc8.modelrouter import ToolCall

    tc = ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    decision = _authorize(
        agent,
        tc,
        frame=frame,
        skill_tool_names=skill_tool_names,
        delegation_depth=int(run.context.get("delegation_depth", 0)),
        skill_thresholds=tuple(
            g.gt
            for s in active
            for g in s.definition.guardrails
            if g.type == "value_threshold" and g.then == "require_approval"
        ),
        tool_policies=effective_tool_policies(frame, agent.narrowing or {}),
        connection_key=conn.name if conn is not None else None,
        tool_scopes=scopes,
        value_spec=value_spec,
        guardrail_attribute_specs=_manifest_guardrail_attributes(conn),
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
            reason_code=decision.reason_code,
            reason_context=decision.context,
        )
        # Park the run and return immediately -- no bounded wait. The adapter
        # polls for the park marker every couple of seconds (runtime.py's
        # _POLL_SECONDS) and tears the container down the moment it sees one, so
        # a wait here can never be outlived by an operator's decision; it only
        # holds this call open while the ground gets pulled out from under it.
        # When that happened the harness never got a tool_result for the call
        # in flight, its transcript ended in a dangling tool_use, and the model
        # endpoint rejected the resumed transcript outright (400). Returning the
        # readable "parked, do not retry" error right away is what lets the
        # harness record a proper tool_result and keeps the transcript
        # resumable. This reverses Decision 1 in docs/superpowers/specs/
        # 2026-07-25-tool-gateway-design.md -- see "Reversed 2026-07-27" there.
        await merge_context(
            db,
            run,
            {
                "isolated_result": {
                    "status": "waiting_for_approval",
                    "output": decision.reason or "",
                }
            },
        )
        await db.commit()
        await _rebind_tenant(db, run.tenant_id)
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
        return _tool_result(
            f"This action needs human approval ({decision.reason or 'threshold reached'}). "
            "The run is parked; it continues once an operator decides. "
            "Do not retry and do not work around it.",
            is_error=True,
        )

    if decision.effect is Effect.DENY:
        return _tool_result(f"ERROR: {decision.reason or 'denied'}", is_error=True)
    if conn is None:
        return _tool_result("ERROR: no tool server available", is_error=True)

    # Idempotency (§8.7 R5) for calls that CHANGE something. Reads are exempt:
    # replaying a search would hide the changes the agent is meant to observe.
    writes = required_right(tc.name, scopes) != "read"

    # Who may touch this record, and how far this run may reach.
    #
    # Both questions are about a CHANGE to one named record, so both are asked
    # here and only here. A search names a kind of record rather than one, and
    # two agents reading the same queue is not a conflict -- it is how a queue
    # works.
    identity = record_identity(tc.name, tc.arguments, focus_spec) if writes else None
    if identity is not None:
        entity, ref = identity
        mine = await held_by_run(
            db, tenant_id=run.tenant_id, entity=entity, record_ref=ref, run_id=run.id
        )
        if not mine:
            limit = records_per_run(frame)
            touched = await records_touched(db, tenant_id=run.tenant_id, run_id=run.id)
            if limit and touched >= limit:
                return await _stop_for_blast_radius(
                    db,
                    run=run,
                    agent=agent,
                    entity=entity,
                    ref=ref,
                    touched=touched,
                    limit=limit,
                )
        holder = await claim_record(
            db,
            tenant_id=run.tenant_id,
            entity=entity,
            record_ref=ref,
            run_id=run.id,
            agent_id=agent.id,
        )
        if holder is not None:
            label = describe_focus(tc.name, tc.arguments, focus_spec) or f"{entity} {ref}"
            # Recorded, or the only evidence that two agents coordinated at all
            # would be the ABSENCE of a duplicate -- which looks exactly like
            # nothing having happened.
            await record_activity(
                db,
                tenant_id=run.tenant_id,
                agent_id=agent.id,
                status="info",
                message=f"Überlässt {label} an {holder.agent_name}",
            )
            await db.commit()
            await _rebind_tenant(db, run.tenant_id)
            return _tool_result(claim_refusal(label, holder), is_error=True)

    if writes and run.task_id is not None:
        replay = await replayed_result(
            db,
            tenant_id=run.tenant_id,
            task_id=run.task_id,
            tool=tc.name,
            arguments=tc.arguments,
        )
        if replay is not None:
            return _tool_result(replay)

    # Before the call, never after: the point is that the recipient is not
    # reached a second time, and a check that ran afterwards would only be able
    # to report it.
    target = outward_target(tc.name, tc.arguments, focus_spec, outward_tools)
    if target is not None and run.task_id is not None:
        if await already_delivered(db, tenant_id=run.tenant_id, task_id=run.task_id, target=target):
            return _tool_result(REFUSAL.format(target=target), is_error=True)

    focus = describe_focus(tc.name, tc.arguments, focus_spec)
    names_record = describes_a_record(tc.name, tc.arguments, focus_spec)
    if focus is not None:
        await note_focus(
            db,
            tenant_id=run.tenant_id,
            agent_id=agent.id,
            task_id=run.task_id,
            focus=focus,
            specific=names_record,
        )
    try:
        # Resolved inside the guard: minting an OAuth-backed token is now part
        # of this, and a failure there belongs to the model as a tool error --
        # not as a 500 out of the whole tools/call.
        env = await _env(conn, db, run.tenant_id)
        headers = resolve_auth_header(cfg, env)
        # Reused across calls: the handshake behind this costs ~3s and the call
        # itself ~50ms, so paying it per call was the whole of the latency. Not
        # reused when the environment carries a minted token that expires.
        # Same wrapping as the tools/list path above, for the same reason.
        command, args = wrap_with_requirements(cfg.get("command", ""), cfg.get("args", []), cfg)
        output = await mcp_pool.call(
            conn.id,
            command=command,
            args=args,
            env=env,
            tool=tc.name,
            arguments=tc.arguments,
            transport=conn.transport,
            server_url=conn.server_url,
            headers=headers,
            http_tools=list(cfg.get("http_tools", [])),
            reusable=not has_oauth_ref(cfg),
        )
    except Exception as exc:  # surface to the model, not as a broken server
        return _tool_result(f"ERROR: {exc}", is_error=True)

    # Now that the call has answered, the record's own name is available -- it is
    # in the RESULT, never in the arguments -- so the card can say WHICH ticket
    # rather than only its number. Deliberately after the call and never in its
    # way: a failed lookup leaves the number, which is what the card had before.
    if focus is not None and names_record:
        title = record_title(output, focus_ref_id(tc.arguments, focus_spec), focus_spec)
        if title:
            await note_focus(
                db,
                tenant_id=run.tenant_id,
                agent_id=agent.id,
                task_id=run.task_id,
                focus=f"{focus} \u201e{title}\u201c",
                specific=True,
                feed=False,  # the feed already has this call; only the card gains
            )

    if target is not None and run.task_id is not None:
        await remember_delivery(db, tenant_id=run.tenant_id, task_id=run.task_id, target=target)
    if writes and run.task_id is not None:
        # Only a SUCCESSFUL side effect is worth replaying; recording a failure
        # would answer a legitimate retry with the old error forever.
        await record_invocation(
            db,
            tenant_id=run.tenant_id,
            task_id=run.task_id,
            tool=tc.name,
            arguments=tc.arguments,
            result=output,
        )
    await db.commit()
    # Fenced, so what a stranger wrote cannot pass itself off as an instruction
    # from oc8. Only the connection's answer -- never this gateway's own
    # refusals, which are the one voice the model must not disregard.
    return _tool_result(fence(output, source=f"{conn.name}:{tc.name}"))


# --------------------------------------------------------------------- route


@router.post("")
@router.post("/")
async def mcp_endpoint(
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
) -> Any:
    run, agent, dept, conns = await _caller(db, principal)
    try:
        body = await request.json()
    except Exception:
        return _error(None, _PARSE_ERROR, "invalid JSON")
    if not isinstance(body, dict):
        return _error(None, _INVALID_REQUEST, "expected a JSON-RPC object")

    method = str(body.get("method", ""))
    rpc_id = body.get("id")
    params = body.get("params") or {}

    # A notification carries no id and MUST NOT be answered with a result; a
    # harness treats an unexpected response as a protocol error.
    if rpc_id is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    if method == "initialize":
        return _result(
            rpc_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            },
        )
    if method == "ping":
        return _result(rpc_id, {})
    if method == "tools/list":
        tools = await _list_tools(db, run=run, agent=agent, dept=dept, conns=conns)
        return _result(rpc_id, {"tools": tools})
    if method == "tools/call":
        name = str(params.get("name", ""))
        if not name:
            return _error(rpc_id, _INVALID_REQUEST, "tools/call requires a name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(rpc_id, _INVALID_REQUEST, "arguments must be an object")
        try:
            payload = await _call_tool(
                db,
                run=run,
                agent=agent,
                dept=dept,
                conns=conns,
                name=name,
                arguments=arguments,
            )
        except HTTPException:
            raise
        except Exception as exc:
            return _error(rpc_id, _INTERNAL_ERROR, str(exc))
        return _result(rpc_id, payload)

    return _error(rpc_id, _METHOD_NOT_FOUND, f"unknown method: {method}")
