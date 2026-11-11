"""Anthropic adapter — Claude messages API with tool use."""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from oc8.modelrouter.levels import EFFORT_FOR_LEVEL
from oc8.modelrouter.http_errors import araise_for_status_with_body, raise_for_status_with_body
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ImagePart,
    NeutralMessage,
    TextPart,
    ToolCall,
    ToolCallDelta,
    Usage,
)

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"


def _content_blocks(content: str | list[Any]) -> list[dict[str, Any]] | str:
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.content_type,
                        "data": base64.b64encode(part.data).decode(),
                    },
                }
            )
    return blocks


#: Block types Anthropic requires back, unmodified, on the assistant turns of
#: an ongoing tool-use exchange when extended thinking is on.
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def _current_exchange_start(messages: list[NeutralMessage]) -> int:
    """Index of the first message after the last plain user turn.

    Anthropic requires thinking blocks back only on the assistant turns of the
    tool-use exchange still in progress; earlier turns' reasoning is not sent
    (it would only be stripped again, at the caller's expense).
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index + 1
    return 0


def _to_anthropic_messages(messages: list[NeutralMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    exchange_start = _current_exchange_start(messages)
    for position, msg in enumerate(messages):
        if msg.role == "system":
            # System messages never carry images in this design -- Anthropic's
            # system param is text-only.
            assert isinstance(msg.content, str)
            system_parts.append(msg.content)
        elif msg.role == "user":
            out.append({"role": "user", "content": _content_blocks(msg.content)})
        elif msg.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if position >= exchange_start and msg.tool_calls:
                # Signed blocks go first and verbatim, or the API rejects the
                # tool result that follows.
                blocks.extend(
                    block
                    for block in msg.replay_blocks
                    if block.get("type") in _THINKING_BLOCK_TYPES
                )
            if msg.content:
                content_blocks = _content_blocks(msg.content)
                if isinstance(content_blocks, str):
                    blocks.append({"type": "text", "text": content_blocks})
                else:
                    blocks.extend(content_blocks)
            for tc in msg.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            out.append({"role": "assistant", "content": blocks})
        elif msg.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id or "",
                            "content": msg.content,
                        }
                    ],
                }
            )
    return "\n".join(system_parts), out


#: Keys this adapter always sets itself. An operator-supplied `extra` key
#: matching one of these is dropped rather than applied -- see _build_payload.

_RESERVED_PAYLOAD_KEYS = frozenset(
    {"model", "max_tokens", "messages", "temperature", "effort", "system", "tools", "stream"}
)


def _build_payload(req: CompletionRequest, *, include_temperature: bool = True) -> dict[str, Any]:
    system, messages = _to_anthropic_messages(req.messages)
    payload: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.params.max_tokens,
        "messages": messages,
    }
    if include_temperature:
        payload["temperature"] = req.params.temperature
    if req.params.reasoning is not None:
        payload["effort"] = EFFORT_FOR_LEVEL[req.params.reasoning]
    elif req.params.effort:
        payload["effort"] = req.params.effort
    if system:
        payload["system"] = system
    if req.tools:
        payload["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in req.tools
        ]
    # Applied last so an operator-typed key can never clobber a field this
    # adapter relies on -- see ModelParams.extra.
    if req.params.extra:
        for key, value in req.params.extra.items():
            if key not in _RESERVED_PAYLOAD_KEYS:
                payload[key] = value
    return payload


# Live, 2026-09: newer Claude models (claude-sonnet-5, claude-opus-5, ...)
# reject `temperature` outright rather than merely ignoring it, and there is
# no way to know in advance which model name will do this next -- a hardcoded
# list of "deprecated on" models would need updating with every release and
# would still miss one. So this is detected from the provider's own words and
# handled by retrying once without the field, rather than by naming models.
_TEMPERATURE_DEPRECATED = re.compile(r'"message"\s*:\s*"temperature is deprecated for this model')


def _is_temperature_deprecated(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code == 400 and bool(_TEMPERATURE_DEPRECATED.search(str(exc)))


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _VERSION,
            "content-type": "application/json",
        }

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        async with httpx.AsyncClient(timeout=180.0) as client:
            try:
                resp = await client.post(
                    _API_URL, json=_build_payload(req), headers=self._headers()
                )
                raise_for_status_with_body(resp)
            except httpx.HTTPStatusError as exc:
                if not _is_temperature_deprecated(exc):
                    raise
                resp = await client.post(
                    _API_URL,
                    json=_build_payload(req, include_temperature=False),
                    headers=self._headers(),
                )
                raise_for_status_with_body(resp)
            data = resp.json()

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        replay_blocks: list[dict[str, Any]] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") in _THINKING_BLOCK_TYPES:
                replay_blocks.append(block)
                if block.get("thinking"):
                    thinking_parts.append(str(block["thinking"]))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}) or {},
                    )
                )
        usage_raw = data.get("usage", {})
        usage = Usage(
            tokens_in=int(usage_raw.get("input_tokens", 0)),
            tokens_out=int(usage_raw.get("output_tokens", 0)),
        )
        reasoning = "\n\n".join(thinking_parts).strip()
        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=data.get("stop_reason", "stop"),
            provider=self.provider,
            model=req.model,
            reasoning=reasoning or None,
            replay_blocks=replay_blocks,
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        """Stream via Anthropic's own SSE event types (message_start,
        content_block_delta, message_delta, ...) -- unlike the OpenAI-shaped
        providers, each payload's meaning depends on its `event:` line, not
        just its `data:` body, and a tool call's id/name arrive once on
        content_block_start while its arguments stream afterward as
        text-fragment `input_json_delta`s on content_block_delta, keyed by
        the block's `index` -- the same index a tool_use block was assigned
        when it started, since Anthropic interleaves text and tool_use
        blocks by position rather than a separate tool-call channel.
        """
        payload = {**_build_payload(req), "stream": True}
        # The rejection arrives from `araise_for_status_with_body` before any
        # SSE line has been parsed, so retrying is safe as long as nothing has
        # reached the caller yet -- `emitted` guards that the same way the
        # fallback chain (fallback.py) already does for a mid-stream failure.
        emitted = False
        try:
            async for chunk in self._stream_once(payload):
                emitted = True
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if emitted or not _is_temperature_deprecated(exc):
                raise
        payload = {**_build_payload(req, include_temperature=False), "stream": True}
        async for chunk in self._stream_once(payload):
            yield chunk

    async def _stream_once(self, payload: dict[str, Any]) -> AsyncIterator[CompletionChunk]:
        # message_start carries input_tokens; message_delta carries only
        # output_tokens (Anthropic never repeats the input count once it's
        # sent). The accumulator treats each chunk's usage as the new
        # cumulative total, not something to add across chunks, so
        # message_delta's own usage chunk must still carry the input count
        # forward or it reads as "0 input tokens" once message_delta's
        # smaller usage object overwrites message_start's.
        tokens_in = 0
        # Thinking blocks under construction, by content-block index: their
        # text and signature arrive as deltas, and only the finished block may
        # be replayed.
        thinking: dict[int, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST", _API_URL, json=payload, headers=self._headers()
            ) as resp:
                await araise_for_status_with_body(resp)
                event_type = ""
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[len("data:") :].strip())
                    except (ValueError, TypeError):
                        logger.debug("skipping unparseable anthropic stream line: %r", line[:120])
                        continue
                    block_done = _track_thinking_block(event_type, data, thinking)
                    if block_done is not None:
                        yield CompletionChunk(replay_blocks=[block_done])
                    chunk = _chunk_from_event(event_type, data)
                    if chunk is None:
                        continue
                    if chunk.usage is not None:
                        if chunk.usage.tokens_in:
                            tokens_in = chunk.usage.tokens_in
                        elif event_type == "message_delta":
                            chunk.usage.tokens_in = tokens_in
                    yield chunk


def _track_thinking_block(
    event_type: str, data: dict[str, Any], open_blocks: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    """Assemble streamed thinking blocks; return one once it is complete."""
    index = int(data.get("index", 0) or 0)
    if event_type == "content_block_start":
        block = data.get("content_block") or {}
        if block.get("type") in _THINKING_BLOCK_TYPES:
            open_blocks[index] = dict(block)
        return None
    if event_type == "content_block_delta" and index in open_blocks:
        delta = data.get("delta") or {}
        block = open_blocks[index]
        if delta.get("type") == "thinking_delta":
            block["thinking"] = str(block.get("thinking") or "") + str(delta.get("thinking", ""))
        elif delta.get("type") == "signature_delta":
            block["signature"] = str(block.get("signature") or "") + str(delta.get("signature", ""))
        return None
    if event_type == "content_block_stop":
        return open_blocks.pop(index, None)
    return None


def _chunk_from_event(event_type: str, data: dict[str, Any]) -> CompletionChunk | None:
    """One (event, data) pair -> a chunk, or None if it carries nothing to forward."""
    if event_type == "message_start":
        usage_raw = (data.get("message") or {}).get("usage") or {}
        if not usage_raw:
            return None
        return CompletionChunk(usage=Usage(tokens_in=int(usage_raw.get("input_tokens", 0) or 0)))

    if event_type == "content_block_start":
        block = data.get("content_block") or {}
        if block.get("type") != "tool_use":
            return None
        return CompletionChunk(
            tool_calls=[
                ToolCallDelta(
                    index=int(data.get("index", 0) or 0),
                    id=block.get("id"),
                    name=block.get("name"),
                )
            ]
        )

    if event_type == "content_block_delta":
        delta = data.get("delta") or {}
        if delta.get("type") == "text_delta":
            return CompletionChunk(text=str(delta.get("text", "")))
        if delta.get("type") == "thinking_delta":
            return CompletionChunk(reasoning=str(delta.get("thinking", "")))
        if delta.get("type") == "input_json_delta":
            return CompletionChunk(
                tool_calls=[
                    ToolCallDelta(
                        index=int(data.get("index", 0) or 0),
                        arguments_fragment=str(delta.get("partial_json", "")),
                    )
                ]
            )
        return None

    if event_type == "message_delta":
        delta = data.get("delta") or {}
        usage_raw = data.get("usage") or {}
        chunk = CompletionChunk()
        carries = False
        if delta.get("stop_reason"):
            chunk.stop_reason = str(delta["stop_reason"])
            carries = True
        if usage_raw:
            # This event's usage is output-tokens-only and cumulative -- the
            # input-token count already arrived on message_start and is not
            # repeated here, so merge rather than overwrite by leaving
            # tokens_in unset (0) and letting the accumulator's own "most
            # recent wins" rule work chunk to chunk instead.
            chunk.usage = Usage(tokens_out=int(usage_raw.get("output_tokens", 0) or 0))
            carries = True
        return chunk if carries else None

    return None
