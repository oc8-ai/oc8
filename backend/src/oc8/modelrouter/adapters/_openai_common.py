# backend/src/oc8/modelrouter/adapters/_openai_common.py
"""Shared OpenAI Chat Completions request/response translation, used by
both the dedicated OpenAI adapter and the generic OpenAI-compatible adapter
(same wire format, different endpoint/auth)."""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from oc8.modelrouter.http_errors import raise_for_status_with_body
from oc8.modelrouter.levels import EFFORT_FOR_LEVEL
from oc8.modelrouter.reasoning import (
    cached_tokens_from,
    reasoning_field,
    reasoning_tokens_from,
    split_think_tags,
)
from oc8.modelrouter.reasoning_support import openai_thinks
from oc8.modelrouter.streaming import stream_openai_sse
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ImagePart,
    NeutralMessage,
    TextPart,
    ToolCall,
    Usage,
)

# Stands in for the answer the model never got to give: a tool result followed
# directly by a fresh user turn. Only an interrupted transcript has that shape --
# ours comes from a run that parked for approval mid-call and resumed later -- and
# a strict chat template rejects it outright (opaas_ai:odoo-gpt via vLLM: 400
# "Unexpected role 'user' after role 'tool'"), which is how an approved action
# silently never happened. Non-empty by necessity: the same endpoint rejects an
# empty assistant turn as "Invalid assistant message".
TOOL_BRIDGE_CONTENT = "(tool result received)"


logger = logging.getLogger(__name__)


def _content_blocks(content: str | list[Any]) -> list[dict[str, Any]] | str:
    """`NeutralMessage.content` -> the Chat Completions content shape.

    A plain string passes through unchanged (the common case, and the shape
    every existing call site already expects). A `list[ContentPart]` becomes
    a list of content blocks -- `{"type": "text", ...}` for a `TextPart` and
    `{"type": "image_url", "image_url": {"url": "data:<content_type>;base64,
    <data>"}}` for an `ImagePart`, OpenAI's own inline-data-URL shape. Same
    pattern as `oc8.modelrouter.adapters.anthropic._content_blocks`.
    """
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{part.content_type};base64,"
                            f"{base64.b64encode(part.data).decode()}"
                        )
                    },
                }
            )
    return blocks


def _content_text(content: str | list[Any]) -> str:
    """Plain text view of content for merged system prompts.

    System turns should not carry images, but if a list-shaped content ever
    appears here, preserve the text parts and drop non-text parts rather than
    sending a non-string system prompt to stricter OpenAI-compatible gateways.
    """
    if isinstance(content, str):
        return content
    return "\n".join(part.text for part in content if isinstance(part, TextPart))


def to_openai_messages(messages: list[NeutralMessage]) -> list[dict[str, Any]]:
    # Some OpenAI-compatible gateways enforce the stricter chat-template rule
    # that the system prompt must be the single first message. oc8 may create
    # several system messages -- base instructions, provenance, memory/knowledge
    # context, trim notices -- so merge and hoist them into the only shape every
    # provider accepts.
    system_parts = [
        _content_text(msg.content)
        for msg in messages
        if msg.role == "system" and _content_text(msg.content).strip()
    ]
    out: list[dict[str, Any]] = (
        [{"role": "system", "content": "\n\n".join(system_parts)}] if system_parts else []
    )
    # A call with no name cannot be dispatched by anyone, and providers reject the
    # whole request over it ("Function name was  but must be a-z…"). One such call
    # from the model poisons the transcript for good: it is replayed on every
    # later turn, so the run fails identically forever and no retry helps.
    # Dropped here rather than only at the point it is produced, so a transcript
    # that is ALREADY carrying one can still be continued.
    dropped_call_ids: set[str] = set()
    for msg in messages:
        if msg.role == "system":
            continue
        if msg.role == "user":
            if out and out[-1]["role"] == "tool":
                out.append({"role": "assistant", "content": TOOL_BRIDGE_CONTENT})
            out.append({"role": msg.role, "content": _content_blocks(msg.content)})
        elif msg.role == "assistant":
            usable = [tc for tc in msg.tool_calls if tc.name.strip()]
            for tc in msg.tool_calls:
                if not tc.name.strip():
                    logger.warning("dropping a tool call with no name (id=%r)", tc.id)
                    dropped_call_ids.add(tc.id)
            content_blocks = _content_blocks(msg.content)
            # `.strip()` only applies to the str case; a non-empty list of
            # blocks (e.g. an image) always counts as "something to say".
            has_content = (
                bool(content_blocks.strip())
                if isinstance(content_blocks, str)
                else bool(content_blocks)
            )
            if not usable and not has_content:
                # Nothing left to say: an assistant turn with neither text nor
                # calls is itself rejected ("Invalid assistant message").
                continue
            entry: dict[str, Any] = {"role": "assistant", "content": content_blocks or None}
            if usable:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in usable
                ]
            out.append(entry)
        elif msg.role == "tool":
            # A result whose call was dropped now refers to nothing, which is
            # invalid in its own right -- the pair goes together.
            if (msg.tool_call_id or "") in dropped_call_ids:
                continue
            out.append(
                {"role": "tool", "tool_call_id": msg.tool_call_id or "", "content": msg.content}
            )
    return _merge_consecutive_turns(out)


def _as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": content}] if content else []


def _join_content(first: Any, second: Any) -> Any:
    if not first:
        return second
    if not second:
        return first
    if isinstance(first, str) and isinstance(second, str):
        return f"{first}\n\n{second}"
    return _as_blocks(first) + _as_blocks(second)


def _merge_consecutive_turns(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Strict chat templates (vLLM/Mistral style) require user and assistant turns
    # to alternate: two user turns in a row are rejected with 400 "conversation
    # roles must alternate", and so are two assistant turns. oc8 sends several
    # user turns in a row as a matter of course (clarify stage, compaction), so
    # fold each run of same-role turns into one. An assistant turn is only folded
    # into the next one when it carries no tool calls of its own -- its calls must
    # stay directly in front of their results. Tool results are never folded:
    # each one answers its own call id.
    merged: list[dict[str, Any]] = []
    for msg in out:
        prev = merged[-1] if merged else None
        same_role = prev is not None and prev["role"] == msg["role"]
        if prev is not None and same_role and msg["role"] == "user":
            prev["content"] = _join_content(prev["content"], msg["content"])
            continue
        if (
            prev is not None
            and same_role
            and msg["role"] == "assistant"
            and not prev.get("tool_calls")
        ):
            prev["content"] = _join_content(prev["content"], msg["content"]) or None
            if msg.get("tool_calls"):
                prev["tool_calls"] = msg["tool_calls"]
            continue
        merged.append(msg)
    return merged


#: Keys this adapter always sets itself. An operator-supplied `extra` key
#: matching one of these is dropped rather than applied -- see build_payload.
_RESERVED_PAYLOAD_KEYS = frozenset({"model", "messages", "temperature", "max_tokens", "tools"})


def build_payload(req: CompletionRequest, *, openai_api: bool = False) -> dict[str, Any]:
    """The Chat Completions body. `openai_api` is api.openai.com itself: it
    takes `max_completion_tokens` for every model (`max_tokens` is deprecated
    there and refused by the reasoning models), and its reasoning models take
    no `temperature`. A gateway keeps `max_tokens`, the only name vLLM,
    LiteLLM and older servers know; `adapt_refused_payload` covers the ones
    that have moved on."""
    payload: dict[str, Any] = {
        "model": req.model,
        "messages": to_openai_messages(req.messages),
    }
    if openai_api:
        payload["max_completion_tokens"] = req.params.max_tokens
        if not openai_thinks(req.model):
            payload["temperature"] = req.params.temperature
    else:
        payload["temperature"] = req.params.temperature
        payload["max_tokens"] = req.params.max_tokens
    # Only a level is mapped. A free-form `effort` alone was never sent here
    # and still is not (spec §6: no level keeps today's behaviour).
    if req.params.reasoning is not None:
        payload["reasoning_effort"] = EFFORT_FOR_LEVEL[req.params.reasoning]
    if req.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in req.tools
        ]
    # Applied last so an operator-typed key can never clobber a field this
    # adapter relies on -- see ModelParams.extra.
    if req.params.extra:
        for key, value in req.params.extra.items():
            if key in _RESERVED_PAYLOAD_KEYS:
                continue
            # api.openai.com gets its budget under this name from the adapter;
            # on a gateway it stays the operator's own key, as before.
            if openai_api and key == "max_completion_tokens":
                continue
            # A level sets `reasoning_effort` itself; without one the operator's
            # own key passes through exactly as before.
            if key == "reasoning_effort" and "reasoning_effort" in payload:
                continue
            payload[key] = value
    return payload


def parse_response(data: dict[str, Any], *, provider: str, model: str) -> CompletionResult:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    raw_calls = message.get("tool_calls") or []
    tool_calls: list[ToolCall] = []
    for c in raw_calls:
        fn = c.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        tool_calls.append(ToolCall(id=c.get("id", ""), name=fn.get("name", ""), arguments=args))
    usage_raw = data.get("usage") or {}
    usage = Usage(
        tokens_in=int(usage_raw.get("prompt_tokens", 0)),
        tokens_out=int(usage_raw.get("completion_tokens", 0)),
        reasoning_tokens=reasoning_tokens_from(usage_raw, "completion_tokens_details"),
        cache_read_tokens=cached_tokens_from(usage_raw, "prompt_tokens_details"),
    )
    # A model may write its reasoning into the answer as <think>...</think>
    # instead of (or as well as) a separate field; either way it is not the
    # answer.
    text, inline_reasoning = split_think_tags(message.get("content") or "")
    reasoning = "\n\n".join(r for r in (reasoning_field(message), inline_reasoning) if r)
    if tool_calls:
        stop_reason = "tool_use"
    elif choice.get("finish_reason") == "length":
        # Kept distinct from "stop": a call cut off by max_tokens is what the
        # runtimes' length retry looks for.
        stop_reason = "length"
    else:
        stop_reason = "stop"
    return CompletionResult(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=stop_reason,
        provider=provider,
        model=model,
        reasoning=reasoning or None,
    )


#: The provider says `max_tokens` is the wrong name. Not any complaint about
#: its value ("max_tokens too large", "must be at least 1"): those are a
#: budget problem, which trim.py and the length retry answer.
_MAX_TOKENS_REFUSED = re.compile(
    r"max_completion_tokens"
    r"|(?:unsupported|not supported)[^.]*\bmax_tokens\b"
    r"|\bmax_tokens\b[^.]*(?:unsupported|not supported)",
    re.IGNORECASE,
)
_TEMPERATURE_REFUSED = re.compile(r"temperature", re.IGNORECASE)
#: A model that reasons by default and takes function tools on Chat Completions
#: only with reasoning switched off (gpt-6.2-sol, gpt-6-astra: "set
#: reasoning_effort to 'none'"). Sending no effort at all is refused as well,
#: so it has to be sent as 'none', not dropped.
_REASONING_NONE_ASKED = re.compile(
    r"reasoning_effort\W+to\W+none|tools with reasoning_effort are not supported",
    re.IGNORECASE,
)
#: Only a malformed or unprocessable request can be a refused parameter.
_PARAM_REFUSAL_STATUSES = frozenset({400, 422})


def asks_for_responses_api(exc: Exception) -> bool:
    """A model that reasons next to function tools only on the Responses API."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code not in _PARAM_REFUSAL_STATUSES:
        return False
    try:
        text = exc.response.text
    except Exception:
        text = str(exc).split(": ", 1)[-1]
    return bool(_REASONING_NONE_ASKED.search(text))


def adapt_refused_payload(
    payload: dict[str, Any], exc: Exception, *, reasoning_none: bool = True
) -> dict[str, Any] | None:
    """`payload` without the parameter the provider just refused, or None if
    this error is not that. Read from the provider's own words, like the
    reasoning retry in router.py: no list of model names could keep up with
    which server wants `max_completion_tokens` or refuses `temperature`.
    Each call removes one key or sets `reasoning_effort` to 'none' once, so
    retrying on it always ends."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response.status_code not in _PARAM_REFUSAL_STATUSES:
        return None
    try:
        text = exc.response.text
    except Exception:
        text = str(exc).split(": ", 1)[-1]
    if (
        reasoning_none
        and payload.get("tools")
        and payload.get("reasoning_effort") != "none"
        and _REASONING_NONE_ASKED.search(text)
    ):
        return {**payload, "reasoning_effort": "none"}
    if "max_tokens" in payload and _MAX_TOKENS_REFUSED.search(text):
        adapted = {k: v for k, v in payload.items() if k != "max_tokens"}
        adapted["max_completion_tokens"] = payload["max_tokens"]
        return adapted
    if "temperature" in payload and _TEMPERATURE_REFUSED.search(text):
        return {k: v for k, v in payload.items() if k != "temperature"}
    return None


async def post_completion(
    url: str, payload: dict[str, Any], headers: dict[str, str], *, reasoning_none: bool = True
) -> dict[str, Any]:
    """POST the completion, once more per parameter the provider refuses."""
    async with httpx.AsyncClient(timeout=180.0) as client:
        while True:
            resp = await client.post(url, json=payload, headers=headers)
            try:
                raise_for_status_with_body(resp)
            except httpx.HTTPStatusError as exc:
                adapted = adapt_refused_payload(payload, exc, reasoning_none=reasoning_none)
                if adapted is None:
                    raise
                payload = adapted
                continue
            result: dict[str, Any] = resp.json()
            return result


async def stream_completion(
    url: str, payload: dict[str, Any], headers: dict[str, str], *, reasoning_none: bool = True
) -> AsyncIterator[CompletionChunk]:
    """`stream_openai_sse`, once more per parameter the provider refuses. A
    refusal arrives before any SSE line, so nothing has reached the caller;
    after the first chunk an error is passed on untouched."""
    while True:
        emitted = False
        try:
            async for chunk in stream_openai_sse(url, payload, headers):
                emitted = True
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            adapted = (
                None
                if emitted
                else adapt_refused_payload(payload, exc, reasoning_none=reasoning_none)
            )
            if adapted is None:
                raise
            payload = adapted
