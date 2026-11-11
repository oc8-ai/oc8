"""Ollama adapter — local, keyless. Uses the /api/chat tool-calling endpoint."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from oc8.modelrouter.http_errors import araise_for_status_with_body, raise_for_status_with_body
from oc8.modelrouter.levels import EFFORT_FOR_LEVEL
from oc8.modelrouter.reasoning import ThinkTagSplitter, split_think_tags
from oc8.modelrouter.reasoning_support import ollama_has_think_levels
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ImagePart,
    NeutralMessage,
    ReasoningLevel,
    TextPart,
    ToolCall,
    ToolCallDelta,
    Usage,
)

logger = logging.getLogger(__name__)


def _message_to_ollama(msg: NeutralMessage) -> dict[str, Any]:
    """`NeutralMessage` -> `/api/chat`'s own message shape.

    Unlike the Anthropic/OpenAI/Responses adapters, Ollama does NOT take
    inline image content blocks: `content` stays a single string (every
    `TextPart` joined together for the list case, unchanged for the plain
    `str` case), and images go in a separate top-level `"images"` array,
    each a RAW base64 string with no `data:image/...;base64,` prefix -- that
    prefix is an OpenAI/Anthropic convention, not Ollama's.
    """
    if isinstance(msg.content, str):
        content: str = msg.content
        images: list[str] = []
    else:
        content = "\n".join(part.text for part in msg.content if isinstance(part, TextPart))
        images = [
            base64.b64encode(part.data).decode()
            for part in msg.content
            if isinstance(part, ImagePart)
        ]
    out: dict[str, Any] = {"role": msg.role, "content": content}
    if images:
        out["images"] = images
    if msg.tool_calls:
        out["tool_calls"] = [
            {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in msg.tool_calls
        ]
    if msg.role == "tool" and msg.name:
        out["name"] = msg.name
    return out


def _think(model: str, level: ReasoningLevel) -> bool | str:
    """Ollama's `think` for `level` (spec 2026-11-11 §6). A model with think
    levels (gpt-oss) ignores true/false, so it gets its level name instead."""
    if ollama_has_think_levels(model):
        return EFFORT_FOR_LEVEL[level]
    return level != "fast"


def _build_payload(req: CompletionRequest, *, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": req.model,
        "messages": [_message_to_ollama(m) for m in req.messages],
        "stream": stream,
        "options": {"temperature": req.params.temperature},
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
    if req.params.reasoning is not None:
        payload["think"] = _think(req.model, req.params.reasoning)
    return payload


def _stop_reason(data: dict[str, Any], *, saw_tool_call: bool) -> str:
    if saw_tool_call:
        return "tool_use"
    # `done_reason: "length"` is a cut-off answer, which the runtimes' length
    # retry needs to tell apart from a finished one.
    return "length" if data.get("done_reason") == "length" else "stop"


class OllamaAdapter:
    provider = "ollama"

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{self._base_url}/api/chat", json=_build_payload(req, stream=False)
            )
            raise_for_status_with_body(resp)
            data = resp.json()

        message = data.get("message", {})
        raw_calls = message.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=f"call_{i}",
                name=c["function"]["name"],
                arguments=c["function"].get("arguments", {}) or {},
            )
            for i, c in enumerate(raw_calls)
        ]
        usage = Usage(
            tokens_in=int(data.get("prompt_eval_count", 0)),
            tokens_out=int(data.get("eval_count", 0)),
        )
        stop_reason = _stop_reason(data, saw_tool_call=bool(tool_calls))
        # Ollama returns a thinking model's reasoning in `message.thinking`
        # when asked to; a model it runs without that support writes <think>
        # tags into the content instead.
        text, inline = split_think_tags(message.get("content", "") or "")
        reasoning = "\n\n".join(
            r for r in (str(message.get("thinking") or "").strip(), inline) if r
        )
        return CompletionResult(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop_reason,
            reasoning=reasoning or None,
            provider=self.provider,
            model=req.model,
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        """Stream /api/chat's response, which is newline-delimited JSON
        objects -- NOT SSE, no `data:` prefix -- one per model step, ending
        with a `"done": true` line that carries the run's totals.

        Ollama's local models report a tool call as one complete object per
        line rather than fragmenting its arguments token by token the way
        OpenAI/Anthropic do, so each call becomes ONE delta carrying its
        whole `arguments_fragment` in one shot -- the same shape
        stream_with_fallback's own single-chunk synthesis already uses for
        a non-streaming adapter's tool calls.
        """
        seen_calls = 0
        saw_tool_call = False
        think = ThinkTagSplitter()
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST", f"{self._base_url}/api/chat", json=_build_payload(req, stream=True)
            ) as resp:
                await araise_for_status_with_body(resp)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except (ValueError, TypeError):
                        logger.debug("skipping unparseable ollama stream line: %r", line[:120])
                        continue
                    message = data.get("message") or {}
                    chunk = CompletionChunk()
                    carries = False
                    if message.get("content"):
                        chunk.text, inline = think.feed(str(message["content"]))
                        chunk.reasoning = inline
                        carries = True
                    if message.get("thinking"):
                        chunk.reasoning += str(message["thinking"])
                        carries = True
                    for raw_call in message.get("tool_calls") or []:
                        fn = raw_call.get("function") or {}
                        chunk.tool_calls.append(
                            ToolCallDelta(
                                index=seen_calls,
                                id=f"call_{seen_calls}",
                                name=fn.get("name"),
                                arguments_fragment=json.dumps(fn.get("arguments") or {}),
                            )
                        )
                        seen_calls += 1
                        saw_tool_call = True
                        carries = True
                    if data.get("done"):
                        held_text, held_reasoning = think.flush()
                        chunk.text += held_text
                        chunk.reasoning += held_reasoning
                        chunk.usage = Usage(
                            tokens_in=int(data.get("prompt_eval_count", 0) or 0),
                            tokens_out=int(data.get("eval_count", 0) or 0),
                        )
                        chunk.stop_reason = _stop_reason(data, saw_tool_call=saw_tool_call)
                        carries = True
                    if carries:
                        yield chunk

    async def embed(self, text: str, model: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._base_url}/api/embeddings", json={"model": model, "prompt": text}
            )
            raise_for_status_with_body(resp)
            data = resp.json()
        return [float(x) for x in data.get("embedding", [])]
