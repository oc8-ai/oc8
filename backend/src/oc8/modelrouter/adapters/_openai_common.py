# backend/src/oc8/modelrouter/adapters/_openai_common.py
"""Shared OpenAI Chat Completions request/response translation, used by
both the dedicated OpenAI adapter and the generic OpenAI-compatible adapter
(same wire format, different endpoint/auth)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from oc8.modelrouter.http_errors import raise_for_status_with_body
from oc8.modelrouter.types import (
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
    return out


#: Keys this adapter always sets itself. An operator-supplied `extra` key
#: matching one of these is dropped rather than applied -- see build_payload.
_RESERVED_PAYLOAD_KEYS = frozenset({"model", "messages", "temperature", "max_tokens", "tools"})


def build_payload(req: CompletionRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": req.model,
        "messages": to_openai_messages(req.messages),
        "temperature": req.params.temperature,
        "max_tokens": req.params.max_tokens,
    }
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
            if key not in _RESERVED_PAYLOAD_KEYS:
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
    usage_raw = data.get("usage", {})
    usage = Usage(
        tokens_in=int(usage_raw.get("prompt_tokens", 0)),
        tokens_out=int(usage_raw.get("completion_tokens", 0)),
    )
    return CompletionResult(
        text=message.get("content") or "",
        tool_calls=tool_calls,
        usage=usage,
        stop_reason="tool_use" if tool_calls else "stop",
        provider=provider,
        model=model,
    )


async def post_completion(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        raise_for_status_with_body(resp)
        result: dict[str, Any] = resp.json()
        return result
