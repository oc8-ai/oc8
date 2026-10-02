"""oc8 as an OpenAI-compatible LLM gateway for agent runtimes (§8.7 R1).

An agent container holds no provider key. It points at this endpoint with its own
run-scoped token and believes it is talking to a normal model API; the control
plane forwards, keeping the model router, BYOK, EU locality, metering and the
§15.4 budget in the path. The relationship pgbouncer has to Postgres.

Mounted under ``/llm`` rather than ``/api/v1`` so it cannot collide with the
operator API and a reverse proxy can expose the two separately.

Errors are translated into the shapes a harness already retries on correctly --
401, 429 + Retry-After, 502 -- never a 200 carrying an error string, which a
harness cannot distinguish from success and will loop on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from oc8 import models as m
from oc8.agents.versioning import pinned_model_config_id, resolve_version
from oc8.api.deps import CurrentPrincipal, DbSession
from oc8.config import get_settings
from oc8.db.session import tenant_session
from oc8.metering import check_budget, record_usage
from oc8.modelrouter import (
    NeutralMessage,
    NeutralTool,
    ToolCall,
    complete_with_fallback,
    estimate_tokens,
    get_model_router,
)
from oc8.modelrouter.fallback import stream_completion_with_fallback
from oc8.modelrouter.http_errors import describe_upstream_error, explain_upstream_error
from oc8.modelrouter.router import ClassificationViolation, TenantKeyRequired
from oc8.modelrouter.sampling import resolve_params
from oc8.modelrouter.types import CompletionResult, Usage

logger = logging.getLogger(__name__)

router = APIRouter()

RUN_SCOPE = "run:"
# How long a harness should wait before retrying a budget refusal. Long enough
# that it stops hammering, short enough that an operator raising the limit takes
# effect without a restart.
_BUDGET_RETRY_AFTER = "60"
# Strong references to in-flight usage writes, so a detached bill cannot be
# garbage-collected before it commits.
_PENDING_BILLS: set[asyncio.Task[None]] = set()


class GatewayCaller(BaseModel):
    """The agent behind a gateway request."""

    tenant_id: uuid.UUID
    agent: m.Agent
    run_id: uuid.UUID
    #: The agent version the run is pinned to (`resolve_version`). Model and
    #: sampling come from here, never the live row: a container calls this
    #: gateway once per turn, and a model switch must not reach a running run.
    pinned: dict[str, Any]

    model_config = {"arbitrary_types_allowed": True}


async def _caller(db: DbSession, principal: CurrentPrincipal) -> GatewayCaller:
    """Resolve the calling agent, or refuse.

    Only a `kind=agent` token scoped to a run may use the gateway. An operator
    token must not become a way to spend a tenant's model budget, and a shared
    key would leave us unable to attribute cost or scope a budget -- so the
    identity is taken from the token, never from the request body.
    """
    if principal.kind != "agent":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "gateway requires an agent token")
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
    return GatewayCaller(
        tenant_id=run.tenant_id,
        agent=agent,
        run_id=run_id,
        pinned=await resolve_version(db, run, agent),
    )


# ------------------------------------------------------------------- inbound


def _to_neutral_messages(raw: list[dict[str, Any]]) -> list[NeutralMessage]:
    """OpenAI Chat Completions messages -> neutral. The reverse of what the
    adapters do on the way out."""
    out: list[NeutralMessage] = []
    for msg in raw:
        role = str(msg.get("role", "user"))
        content = msg.get("content")
        # Content may be a list of parts (text/image). Only text is forwarded;
        # concatenating is better than dropping the turn entirely.
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        if role == "tool":
            out.append(
                NeutralMessage(
                    role="tool",
                    content=str(content or ""),
                    tool_call_id=str(msg.get("tool_call_id") or ""),
                    name=msg.get("name"),
                )
            )
            continue
        calls: list[ToolCall] = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            calls.append(
                ToolCall(id=str(c.get("id", "")), name=str(fn.get("name", "")), arguments=args)
            )
        if role not in ("system", "user", "assistant"):
            role = "user"
        out.append(
            NeutralMessage(
                role=role,  # type: ignore[arg-type]
                content=str(content or ""),
                tool_calls=calls,
            )
        )
    return out


def _to_neutral_tools(raw: list[dict[str, Any]]) -> list[NeutralTool]:
    tools: list[NeutralTool] = []
    for t in raw:
        fn = t.get("function") or t
        name = fn.get("name")
        if not name:
            continue
        tools.append(
            NeutralTool(
                name=str(name),
                description=str(fn.get("description") or ""),
                parameters=fn.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    return tools


# ------------------------------------------------------------------ outbound


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _tool_calls_out(calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "id": tc.id or f"call_{i}",
            "type": "function",
            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
        }
        for i, tc in enumerate(calls)
    ]


def _finish_reason(stop_reason: str, has_calls: bool) -> str:
    if has_calls or stop_reason == "tool_use":
        return "tool_calls"
    return "stop" if stop_reason in ("stop", "") else stop_reason


# ------------------------------------------------------------------ metering


async def _bill(
    db: DbSession,
    caller: GatewayCaller,
    *,
    request_id: uuid.UUID,
    provider: str,
    model: str,
    usage: Usage,
) -> None:
    """Record what this call consumed, on a session of its OWN.

    Deliberately not the request's session. ``tenant_session`` commits on a clean
    exit and rolls back on any exception -- including the CancelledError raised
    when a client disconnects mid-stream. Billing on the request's transaction
    therefore vanished with it, and an aborted stream cost nothing at all
    (observed live). A container-per-agent runtime is killed routinely by its idle
    timeout, so that is the common case, not an edge one -- and the §15.4 budget is
    the only thing between a runaway agent and the bill.

    A separate session is necessary but NOT sufficient: when the request task is
    cancelled, every await inside a ``finally`` raises CancelledError at once --
    including opening that session. So the write runs as a DETACHED task and is
    only awaited through a shield. On the normal path the shield awaits it to
    completion (so callers and tests see the row immediately); on cancellation the
    outer await is cancelled while the detached task carries on and commits.

    ``db`` stays in the signature so the caller's tenant binding is obvious at
    every call site, but it is intentionally unused for the write.
    """
    _ = db
    agent_id = caller.agent.id
    department_id = caller.agent.department_id
    tenant_id = caller.tenant_id
    tokens_in, tokens_out = usage.tokens_in, usage.tokens_out

    async def _write() -> None:
        async with tenant_session(tenant_id) as own:
            await record_usage(
                own,
                tenant_id=tenant_id,
                request_id=request_id,
                model=model,
                provider=provider,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                agent_id=agent_id,
                department_id=department_id,
            )

    task = asyncio.ensure_future(_write())
    # Hold a reference: a task only weakly referenced can be garbage-collected
    # before it runs, which would lose the bill in exactly the case this exists for.
    _PENDING_BILLS.add(task)
    task.add_done_callback(_PENDING_BILLS.discard)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The shield keeps `task` running to completion; re-raise so the
        # cancellation is still honoured. Swallowing it here would make the
        # request un-cancellable, which is a worse bug than the one being fixed.
        raise


# -------------------------------------------------------------------- routes


async def _prepare(
    db: DbSession,
    caller: GatewayCaller,
    *,
    messages: list[NeutralMessage],
    tools: list[NeutralTool],
) -> tuple[dict[str, Any], uuid.UUID, str, str, m.ModelConfig | None]:
    """Resolve model + sampling, apply the budget gate, build the call arguments.

    Shared by both inbound surfaces so policy cannot differ between them: an
    agent must not be able to pick the endpoint whose budget check is weaker.
    """
    agent = caller.agent
    pinned_model_id = pinned_model_config_id(caller.pinned)
    model_config = await db.get(m.ModelConfig, pinned_model_id) if pinned_model_id else None
    settings = get_settings()
    if model_config is not None:
        provider, model = model_config.provider, model_config.model
    else:
        provider = (agent.presentation or {}).get("provider", settings.default_model_provider)
        model = settings.default_model

    # Budget BEFORE the spend. 429 + Retry-After, because that is what a harness
    # backs off from; a 200 carrying an error string would be indistinguishable
    # from success and it would loop.
    budget = await check_budget(db, tenant_id=caller.tenant_id, department_id=agent.department_id)
    if budget.hard_exceeded:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "token budget exceeded for this tenant",
            headers={"Retry-After": _BUDGET_RETRY_AFTER},
        )

    request_id = uuid.uuid4()
    common: dict[str, Any] = dict(
        tenant_id=caller.tenant_id,
        agent_id=agent.id,
        primary=model_config,
        no_config_provider=provider,
        no_config_model=model,
        messages=messages,
        tools=tools,
        params=resolve_params(model_config, agent=agent, definition=caller.pinned["definition"]),
        request_id=request_id,
        contains_restricted=False,
    )
    return common, request_id, provider, model, model_config


async def _complete_or_raise(db: DbSession, common: dict[str, Any]) -> CompletionResult:
    """Run the completion, translating our failures into provider-shaped ones."""
    try:
        return await complete_with_fallback(db, get_model_router(), **common)
    except ClassificationViolation as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except TenantKeyRequired as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # upstream failed after the chain was exhausted
        # LOG it too, not only in the 502 body. A harness usually reports "502 from
        # the gateway" and nothing else, so the provider's own reason has to be
        # readable server-side or the failure has to be reproduced by hand.
        detail = describe_upstream_error(exc)
        # Some upstream errors say the opposite of what is wrong -- a conversation
        # past the context window comes back as a complaint about a negative
        # max_tokens, which reads like a broken request. Where we can say it
        # plainly, we do; everything else keeps the provider's own words, which
        # are what a person needs to look it up.
        explained = explain_upstream_error(detail)
        logger.warning("llm gateway upstream failed: %s", explained or detail)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, explained or detail) from exc


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
) -> Any:
    caller = await _caller(db, principal)
    body: dict[str, Any] = await request.json()

    messages = _to_neutral_messages(list(body.get("messages") or []))
    tools = _to_neutral_tools(list(body.get("tools") or []))
    wants_stream = bool(body.get("stream"))

    # The agent's ModelConfig decides, NOT the requested model (§9.2/9.4): a
    # runtime must not be able to spend a tenant's money on a model the operator
    # did not choose. The resolved model is echoed back so a harness can log what
    # actually ran rather than what it asked for.
    common, request_id, provider, model, _cfg = await _prepare(
        db, caller, messages=messages, tools=tools
    )

    if not wants_stream:
        result = await _complete_or_raise(db, common)
        await _bill(
            db,
            caller,
            request_id=request_id,
            provider=result.provider,
            model=result.model,
            usage=result.usage,
        )
        calls = _tool_calls_out(result.tool_calls)
        message: dict[str, Any] = {"role": "assistant", "content": result.text or None}
        if calls:
            message["tool_calls"] = calls
        return {
            "id": _completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": _finish_reason(result.stop_reason, bool(calls)),
                }
            ],
            "usage": {
                "prompt_tokens": result.usage.tokens_in,
                "completion_tokens": result.usage.tokens_out,
                "total_tokens": result.usage.tokens_in + result.usage.tokens_out,
            },
        }

    return StreamingResponse(
        _sse(db, caller, common=common, request_id=request_id, provider=provider, model=model),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


async def _sse(
    db: DbSession,
    caller: GatewayCaller,
    *,
    common: dict[str, Any],
    request_id: uuid.UUID,
    provider: str,
    model: str,
) -> AsyncGenerator[str, None]:
    """Render neutral chunks as OpenAI streaming SSE, and bill at the end.

    Usage can only be recorded once the stream finishes -- that is where an
    upstream reports it. If it never does, an estimate is billed rather than
    zero: a zero would hide a runaway agent from its budget.

    The terminal frames are emitted on the normal path, and ``finally`` only bills.
    It must not yield: during ``aclose()`` (a disconnected client) a yield raises
    "async generator ignored GeneratorExit" and takes the billing down with it --
    exactly the case billing here is meant to cover.
    """
    cid = _completion_id()
    created = int(time.time())

    def frame(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    reported: Usage | None = None
    text_len = 0
    yield frame({"role": "assistant", "content": ""})
    try:
        async for chunk in stream_completion_with_fallback(db, get_model_router(), **common):
            if chunk.usage is not None:
                reported = chunk.usage
            if chunk.text:
                text_len += len(chunk.text)
                yield frame({"content": chunk.text})
            for d in chunk.tool_calls:
                fn: dict[str, Any] = {}
                if d.name is not None:
                    fn["name"] = d.name
                if d.arguments_fragment:
                    fn["arguments"] = d.arguments_fragment
                entry: dict[str, Any] = {"index": d.index, "function": fn}
                if d.id is not None:
                    entry["id"] = d.id
                    entry["type"] = "function"
                yield frame({"tool_calls": [entry]})
            if chunk.stop_reason:
                yield frame({}, finish=_finish_reason(chunk.stop_reason, False))
        yield "data: [DONE]\n\n"
    except Exception as exc:  # the stream died; tell the client in-band
        # The response is already 200 with headers sent, so an HTTP status is no
        # longer available. An error frame is the only way left to say it -- and a
        # harness routinely swallows one, so it is logged as well.
        detail = explain_upstream_error(describe_upstream_error(exc)) or describe_upstream_error(
            exc
        )
        logger.warning("llm gateway stream failed: %s", detail)
        yield f"data: {json.dumps({'error': {'message': detail, 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        usage = reported or Usage(tokens_in=0, tokens_out=estimate_tokens("x" * text_len))
        await _bill(db, caller, request_id=request_id, provider=provider, model=model, usage=usage)


# ------------------------------------------------- Anthropic Messages surface
#
# The endpoint a Claude Agent SDK runtime points at via ANTHROPIC_BASE_URL. NOT an
# alias of the OpenAI one: `system` is a top-level field, tool results ride inside
# USER messages as blocks, tools declare `input_schema`, and the streaming event
# vocabulary is different. Each of those, mapped wrongly, loses information
# silently -- the run behaves stupidly rather than failing, which is worse.


def _system_text(raw: Any) -> str:
    """The `system` field, which may be a string or a list of text blocks."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "\n\n".join(
            str(b.get("text", "")) for b in raw if isinstance(b, dict) and b.get("text")
        )
    return ""


def _anthropic_to_neutral(body: dict[str, Any]) -> list[NeutralMessage]:
    out: list[NeutralMessage] = []
    system = _system_text(body.get("system"))
    if system:
        # A field here, a message in our neutral form. Dropping it would strip the
        # agent's whole instruction set with nothing in any log to explain the
        # resulting behaviour.
        out.append(NeutralMessage(role="system", content=system))

    for msg in body.get("messages") or []:
        role = str(msg.get("role", "user"))
        content = msg.get("content")
        if isinstance(content, str):
            out.append(
                NeutralMessage(role="user" if role != "assistant" else "assistant", content=content)
            )
            continue

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        arguments=block.get("input") or {},
                    )
                )
            elif btype == "tool_result":
                # Its own role in our form. Left as user text, the model could not
                # tell its call had run and would simply call the tool again.
                inner = block.get("content")
                if isinstance(inner, list):
                    inner = "".join(str(p.get("text", "")) for p in inner if isinstance(p, dict))
                out.append(
                    NeutralMessage(
                        role="tool",
                        content=str(inner if inner is not None else ""),
                        tool_call_id=str(block.get("tool_use_id", "")),
                    )
                )
        if text_parts or calls:
            out.append(
                NeutralMessage(
                    role="assistant" if role == "assistant" else "user",
                    content="".join(text_parts),
                    tool_calls=calls,
                )
            )
    return out


def _anthropic_tools(raw: list[dict[str, Any]]) -> list[NeutralTool]:
    tools: list[NeutralTool] = []
    for t in raw:
        name = t.get("name")
        if not name:
            continue
        tools.append(
            NeutralTool(
                name=str(name),
                description=str(t.get("description") or ""),
                # `input_schema` here, not `parameters`. Reading the wrong key
                # gives every tool an empty schema and the model calls them blind.
                parameters=t.get("input_schema") or {"type": "object", "properties": {}},
            )
        )
    return tools


def _anthropic_stop_reason(stop_reason: str, has_calls: bool) -> str:
    if has_calls or stop_reason == "tool_use":
        return "tool_use"
    if stop_reason in ("length", "max_tokens"):
        return "max_tokens"
    return "end_turn"


def _message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


@router.post("/v1/messages")
async def messages(
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
) -> Any:
    caller = await _caller(db, principal)
    body: dict[str, Any] = await request.json()

    neutral = _anthropic_to_neutral(body)
    tools = _anthropic_tools(list(body.get("tools") or []))
    common, request_id, provider, model, _cfg = await _prepare(
        db, caller, messages=neutral, tools=tools
    )

    if not body.get("stream"):
        result = await _complete_or_raise(db, common)
        await _bill(
            db,
            caller,
            request_id=request_id,
            provider=result.provider,
            model=result.model,
            usage=result.usage,
        )
        blocks: list[dict[str, Any]] = []
        if result.text:
            blocks.append({"type": "text", "text": result.text})
        for tc in result.tool_calls:
            if not tc.name.strip():
                # Never hand a harness a call it cannot name. It would store the
                # block and replay it on every later turn, and the provider
                # rejects the whole request over it ("Function name was  but must
                # be a-z…") -- one malformed call from the model, and the session
                # is dead for good. Observed live 2026-07-27.
                logger.warning("dropping a nameless tool call from the model (id=%r)", tc.id)
                continue
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id or _message_id(),
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )
        return {
            "id": _message_id(),
            "type": "message",
            "role": "assistant",
            "model": result.model,
            "content": blocks,
            "stop_reason": _anthropic_stop_reason(result.stop_reason, bool(result.tool_calls)),
            "stop_sequence": None,
            "usage": {
                "input_tokens": result.usage.tokens_in,
                "output_tokens": result.usage.tokens_out,
            },
        }

    return StreamingResponse(
        _messages_sse(
            db, caller, common=common, request_id=request_id, provider=provider, model=model
        ),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


async def _messages_sse(
    db: DbSession,
    caller: GatewayCaller,
    *,
    common: dict[str, Any],
    request_id: uuid.UUID,
    provider: str,
    model: str,
) -> AsyncGenerator[str, None]:
    """Render neutral chunks as Anthropic streaming events.

    A harness drives its state machine off these names, so message_start and
    message_stop must always bracket the stream -- including on failure, or the
    client waits forever for an end that never comes.
    """
    mid = _message_id()

    def event(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps({'type': name, **payload})}\n\n"

    reported: Usage | None = None
    text_len = 0
    # Index of the currently open content block, and what kind it is. Text and
    # tool_use cannot share a block, so a switch closes the previous one.
    open_index = -1
    open_kind: str | None = None
    stop_reason = "end_turn"

    yield event(
        "message_start",
        {
            "message": {
                "id": mid,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        },
    )
    try:
        async for chunk in stream_completion_with_fallback(db, get_model_router(), **common):
            if chunk.usage is not None:
                reported = chunk.usage
            if chunk.text:
                if open_kind != "text":
                    if open_kind is not None:
                        yield event("content_block_stop", {"index": open_index})
                    open_index += 1
                    open_kind = "text"
                    yield event(
                        "content_block_start",
                        {"index": open_index, "content_block": {"type": "text", "text": ""}},
                    )
                text_len += len(chunk.text)
                yield event(
                    "content_block_delta",
                    {"index": open_index, "delta": {"type": "text_delta", "text": chunk.text}},
                )
            for d in chunk.tool_calls:
                # A fragment with an id starts a new block; one without continues
                # the open one.
                if d.id is not None or open_kind != "tool_use":
                    if open_kind is not None:
                        yield event("content_block_stop", {"index": open_index})
                    open_index += 1
                    open_kind = "tool_use"
                    yield event(
                        "content_block_start",
                        {
                            "index": open_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": d.id or f"toolu_{open_index}",
                                "name": d.name or "",
                                "input": {},
                            },
                        },
                    )
                if d.arguments_fragment:
                    yield event(
                        "content_block_delta",
                        {
                            "index": open_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": d.arguments_fragment,
                            },
                        },
                    )
            if chunk.stop_reason:
                stop_reason = _anthropic_stop_reason(chunk.stop_reason, open_kind == "tool_use")
        async for frame in _close_message(
            open_kind, open_index, stop_reason, reported, text_len, event
        ):
            yield frame
    except Exception as exc:
        # Headers and 200 are long gone, so this is the only way left to say it --
        # plus the log, since an in-band error frame is easy for a harness to drop.
        detail = describe_upstream_error(exc)
        logger.warning("llm gateway stream failed: %s", detail)
        yield event("error", {"error": {"type": "upstream_error", "message": detail}})
        async for frame in _close_message(
            open_kind, open_index, stop_reason, reported, text_len, event
        ):
            yield frame
    finally:
        # Billing only -- never a yield. During aclose() (a disconnected client) a
        # yield raises "async generator ignored GeneratorExit" and takes the
        # billing with it, which is precisely the case this exists to cover.
        usage = reported or Usage(tokens_in=0, tokens_out=estimate_tokens("x" * text_len))
        await _bill(db, caller, request_id=request_id, provider=provider, model=model, usage=usage)


async def _close_message(
    open_kind: str | None,
    open_index: int,
    stop_reason: str,
    reported: Usage | None,
    text_len: int,
    event: Any,
) -> AsyncIterator[str]:
    """The frames that must bracket the end of a message stream.

    A harness waits for message_stop; without it the turn never completes.
    """
    if open_kind is not None:
        yield event("content_block_stop", {"index": open_index})
    usage = reported or Usage(tokens_in=0, tokens_out=estimate_tokens("x" * text_len))
    yield event(
        "message_delta",
        {
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage.tokens_out},
        },
    )
    yield event("message_stop", {})


# ------------------------------------------------- OpenAI Responses surface
#
# The endpoint codex-cli points at via OPENAI_BASE_URL + wire_api="responses"
# (config.toml) -- see plugins/codex_runtime/runtime/runtime.py. NOT an alias
# of /v1/chat/completions: input is a flat array of typed ITEMS (message,
# function_call, function_call_output), not role/content turns; the system
# prompt rides in a top-level `instructions` field, not the first message;
# tool definitions are flat (`{"type":"function","name":...}`), not nested
# under a `function` key; and codex-cli sends `stream: true` unconditionally
# (verified live against codex-cli 0.147.0, no non-streaming path exercised
# in practice), so the SSE event vocabulary below is what actually matters --
# a plain JSON response would never be reached.
#
# Request/response shapes captured by temporarily logging codex-cli's real
# traffic against this route (see git history if that capture is ever needed
# again) rather than transcribed from memory of OpenAI's docs, given how
# easy a subtly wrong field name is to get away with until a strict client
# actually parses it.


def _responses_input_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(p.get("text", ""))
        for p in content
        if isinstance(p, dict) and p.get("type") in ("input_text", "output_text")
    )


def _responses_to_neutral(body: dict[str, Any]) -> list[NeutralMessage]:
    out: list[NeutralMessage] = []
    instructions = body.get("instructions")
    if instructions:
        # A top-level field here, not the first turn -- see _anthropic_to_neutral's
        # identical `system` handling, the same shape under a different name.
        out.append(NeutralMessage(role="system", content=str(instructions)))

    raw_input = body.get("input")
    items = (
        raw_input
        if isinstance(raw_input, list)
        else [{"type": "message", "role": "user", "content": raw_input}]
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")
        if itype == "message":
            role = item.get("role", "user")
            # "developer" is this API's system-turn role for mid-conversation
            # instructions (codex sends its skills/permissions block this way,
            # observed live) -- folded into "system" like the top-level
            # `instructions` field above, since neutral has no fourth role.
            neutral_role: Literal["system", "user", "assistant"] = (
                "assistant" if role == "assistant" else "system" if role == "developer" else "user"
            )
            out.append(
                NeutralMessage(
                    role=neutral_role, content=_responses_input_text(item.get("content"))
                )
            )
        elif itype == "function_call":
            # The model's own prior tool call, replayed back on the next turn --
            # arguments arrive as a JSON STRING here, unlike Chat Completions'
            # already-nested dict.
            try:
                args = json.loads(item.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            out.append(
                NeutralMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=str(item.get("call_id") or item.get("id") or ""),
                            name=str(item.get("name") or ""),
                            arguments=args,
                        )
                    ],
                )
            )
        elif itype == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                output = "".join(str(p.get("text", "")) for p in output if isinstance(p, dict))
            out.append(
                NeutralMessage(
                    role="tool",
                    content=str(output if output is not None else ""),
                    tool_call_id=str(item.get("call_id") or ""),
                )
            )
        # Any other item type (reasoning, custom_tool_call, ...) is silently
        # skipped: the model gets a slightly shorter history rather than a
        # 500 on a shape this gateway does not yet need to round-trip.
    return out


def _responses_tools(raw: list[dict[str, Any]]) -> list[NeutralTool]:
    """Flat, unlike Chat Completions' `{"type":"function","function":{...}}` --
    codex's own tools arrive as `{"type":"function","name":...,"parameters":...}`
    directly, verified live. Reading the wrong shape here silently gives every
    tool an empty name and the model can never call one."""
    tools: list[NeutralTool] = []
    for t in raw:
        name = t.get("name")
        if not name:
            continue
        tools.append(
            NeutralTool(
                name=str(name),
                description=str(t.get("description") or ""),
                parameters=t.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    return tools


def _response_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _response_item_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _function_call_item_id() -> str:
    return f"fc_{uuid.uuid4().hex[:24]}"


def _responses_output(result: CompletionResult) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if result.text:
        output.append(
            {
                "type": "message",
                "id": _response_item_id(),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": result.text, "annotations": []}],
            }
        )
    for tc in result.tool_calls:
        output.append(
            {
                "type": "function_call",
                "id": _function_call_item_id(),
                "call_id": tc.id or _function_call_item_id(),
                "name": tc.name,
                "arguments": json.dumps(tc.arguments),
                "status": "completed",
            }
        )
    return output


def _responses_object(
    *, response_id: str, model: str, status: str, output: list[dict[str, Any]], usage: Usage
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": usage.tokens_in,
            "output_tokens": usage.tokens_out,
            "total_tokens": usage.tokens_in + usage.tokens_out,
        },
    }


@router.post("/v1/responses")
async def responses(
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
) -> Any:
    caller = await _caller(db, principal)
    body: dict[str, Any] = await request.json()

    neutral = _responses_to_neutral(body)
    tools = _responses_tools(list(body.get("tools") or []))
    common, request_id, provider, model, _cfg = await _prepare(
        db, caller, messages=neutral, tools=tools
    )

    if not body.get("stream"):
        result = await _complete_or_raise(db, common)
        await _bill(
            db,
            caller,
            request_id=request_id,
            provider=result.provider,
            model=result.model,
            usage=result.usage,
        )
        return _responses_object(
            response_id=_response_id(),
            model=result.model,
            status="completed",
            output=_responses_output(result),
            usage=result.usage,
        )

    return StreamingResponse(
        _responses_sse(
            db, caller, common=common, request_id=request_id, provider=provider, model=model
        ),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


async def _responses_sse(
    db: DbSession,
    caller: GatewayCaller,
    *,
    common: dict[str, Any],
    request_id: uuid.UUID,
    provider: str,
    model: str,
) -> AsyncGenerator[str, None]:
    """Render neutral chunks as Responses API streaming events.

    Every event carries a monotonic `sequence_number` (the real API does;
    codex-rs is a strict Rust parser, so a field it expects but does not get
    is safer to include defensively than to omit on the assumption it is
    unused). response.created and response.completed bracket the stream on
    every path, mirroring message_start/message_stop's role in
    _messages_sse -- without a terminal event a harness waits forever.
    """
    rid = _response_id()
    seq = 0

    def event(name: str, payload: dict[str, Any]) -> str:
        nonlocal seq
        seq += 1
        data = {"type": name, "sequence_number": seq, **payload}
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    reported: Usage | None = None
    text_len = 0
    # Mirrors _messages_sse's open_kind/open_index: at most one output item
    # is "open" (added but not yet done) at a time. -1 index means "the next
    # item added gets index 0" (there is no item -1).
    open_kind: str | None = None
    open_index = -1
    open_item_id = ""
    text_acc = ""
    args_acc = ""
    tool_name = ""
    tool_call_id = ""
    # Populated as each item closes, so response.completed's own `output`
    # reflects what actually streamed -- a client that trusts the final
    # snapshot over replaying every incremental event (reasonable: that is
    # what the snapshot is FOR) would otherwise see an empty response that
    # every preceding event just finished describing.
    final_output: list[dict[str, Any]] = []

    def close_open() -> AsyncIterator[str]:
        # A generator, not a plain function, so it can `yield` -- called
        # inline below with `async for ... in close_open()`.
        async def _gen() -> AsyncIterator[str]:
            nonlocal open_kind
            if open_kind == "text":
                yield event(
                    "response.output_text.done",
                    {
                        "item_id": open_item_id,
                        "output_index": open_index,
                        "content_index": 0,
                        "text": text_acc,
                    },
                )
                yield event(
                    "response.content_part.done",
                    {
                        "item_id": open_item_id,
                        "output_index": open_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": text_acc, "annotations": []},
                    },
                )
                item = {
                    "type": "message",
                    "id": open_item_id,
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_acc, "annotations": []}],
                }
                final_output.append(item)
                yield event(
                    "response.output_item.done",
                    {"output_index": open_index, "item": item},
                )
            elif open_kind == "function_call":
                yield event(
                    "response.function_call_arguments.done",
                    {"item_id": open_item_id, "output_index": open_index, "arguments": args_acc},
                )
                item = {
                    "type": "function_call",
                    "id": open_item_id,
                    "call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": args_acc,
                    "status": "completed",
                }
                final_output.append(item)
                yield event(
                    "response.output_item.done",
                    {"output_index": open_index, "item": item},
                )
            open_kind = None

        return _gen()

    yield event(
        "response.created",
        {
            "response": _responses_object(
                response_id=rid,
                model=model,
                status="in_progress",
                output=[],
                usage=Usage(),
            )
        },
    )
    try:
        async for chunk in stream_completion_with_fallback(db, get_model_router(), **common):
            if chunk.usage is not None:
                reported = chunk.usage
            if chunk.text:
                if open_kind != "text":
                    async for frame in close_open():
                        yield frame
                    open_index += 1
                    open_kind = "text"
                    open_item_id = _response_item_id()
                    text_acc = ""
                    yield event(
                        "response.output_item.added",
                        {
                            "output_index": open_index,
                            "item": {
                                "type": "message",
                                "id": open_item_id,
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [],
                            },
                        },
                    )
                    yield event(
                        "response.content_part.added",
                        {
                            "item_id": open_item_id,
                            "output_index": open_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        },
                    )
                text_len += len(chunk.text)
                text_acc += chunk.text
                yield event(
                    "response.output_text.delta",
                    {
                        "item_id": open_item_id,
                        "output_index": open_index,
                        "content_index": 0,
                        "delta": chunk.text,
                    },
                )
            for d in chunk.tool_calls:
                # A fragment carrying an id starts a new call; one without
                # continues the currently open one -- same convention
                # _messages_sse's tool_use handling uses.
                if d.id is not None or open_kind != "function_call":
                    async for frame in close_open():
                        yield frame
                    open_index += 1
                    open_kind = "function_call"
                    open_item_id = _function_call_item_id()
                    tool_call_id = d.id or open_item_id
                    tool_name = d.name or ""
                    args_acc = ""
                    yield event(
                        "response.output_item.added",
                        {
                            "output_index": open_index,
                            "item": {
                                "type": "function_call",
                                "id": open_item_id,
                                "call_id": tool_call_id,
                                "name": tool_name,
                                "arguments": "",
                                "status": "in_progress",
                            },
                        },
                    )
                if d.name and not tool_name:
                    tool_name = d.name
                if d.arguments_fragment:
                    args_acc += d.arguments_fragment
                    yield event(
                        "response.function_call_arguments.delta",
                        {
                            "item_id": open_item_id,
                            "output_index": open_index,
                            "delta": d.arguments_fragment,
                        },
                    )
        async for frame in close_open():
            yield frame
        usage = reported or Usage(tokens_in=0, tokens_out=estimate_tokens("x" * text_len))
        final = _responses_object(
            response_id=rid,
            model=model,
            status="completed",
            output=final_output,
            usage=usage,
        )
        yield event("response.completed", {"response": final})
    except Exception as exc:
        # Headers and 200 are long gone, so this is the only way left to say
        # it -- plus the log, since an in-band error frame is easy for a
        # harness to drop. No response.failed here on purpose: unlike
        # Anthropic's error event, the Responses API expects the response
        # object's own top-level `error` field on a genuinely failed
        # response.completed, not a separate event type.
        detail = describe_upstream_error(exc)
        logger.warning("llm gateway responses stream failed: %s", detail)
        usage = reported or Usage(tokens_in=0, tokens_out=estimate_tokens("x" * text_len))
        failed = _responses_object(
            response_id=rid,
            model=model,
            status="failed",
            output=[],
            usage=usage,
        )
        failed["error"] = {"message": detail, "type": "upstream_error"}
        yield event("response.completed", {"response": failed})
    finally:
        usage = reported or Usage(tokens_in=0, tokens_out=estimate_tokens("x" * text_len))
        await _bill(db, caller, request_id=request_id, provider=provider, model=model, usage=usage)


@router.get("/v1/models")
async def list_models(db: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """The models this tenant may use.

    A harness probes this on startup; an empty or failing list makes it give up
    before the first turn. Only the tenant's configured models are listed -- the
    gateway will not honour a model the operator did not choose anyway.
    """
    from sqlalchemy import select

    caller = await _caller(db, principal)
    rows = (
        (await db.execute(select(m.ModelConfig).where(m.ModelConfig.tenant_id == caller.tenant_id)))
        .scalars()
        .all()
    )
    seen: list[str] = []
    for cfg in rows:
        if cfg.model not in seen:
            seen.append(cfg.model)
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": int(time.time()), "owned_by": "oc8"}
            for name in seen
        ],
    }
