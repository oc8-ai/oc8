"""Streaming completions through the router.

The router was request/response only. A real agent harness streams, so the LLM
gateway (§8.7 R1) needs this path -- and it needs it without losing the two things
the control plane exists to do: report usage and stay provider-neutral.

Two rules encoded here:

- **Usage must be asked for.** An OpenAI-compatible upstream reports usage on a
  stream only when the request carries ``stream_options: {include_usage: true}``.
  Omitting it means every streamed call bills zero, which makes the §15.4 budget
  blind precisely for the traffic a self-driving runtime generates.
- **No provider may silently break.** An adapter without a ``stream`` method
  degrades to a single chunk holding the whole answer, rather than failing or
  being skipped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

import httpx

from oc8.modelrouter.http_errors import araise_for_status_with_body
from oc8.modelrouter.reasoning import ThinkTagSplitter, reasoning_field, reasoning_tokens_from
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ToolCallDelta,
    Usage,
)

logger = logging.getLogger(__name__)

_DONE = "[DONE]"
# Rough characters-per-token, used ONLY when a provider reported no usage at all.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """A deliberately crude token estimate for output nothing reported.

    Exists so an unreported stream bills a non-zero number rather than nothing: a
    zero would hide a runaway agent from its budget, which is worse than being
    approximately right. Never use this when the provider did report usage.
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


@runtime_checkable
class SupportsStreaming(Protocol):
    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]: ...


def _chunk_from_openai(data: dict[str, Any]) -> CompletionChunk | None:
    """One SSE payload -> a chunk, or None if it carries nothing we forward."""
    chunk = CompletionChunk()
    carries = False

    usage_raw = data.get("usage")
    if isinstance(usage_raw, dict):
        chunk.usage = Usage(
            tokens_in=int(usage_raw.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage_raw.get("completion_tokens", 0) or 0),
            reasoning_tokens=reasoning_tokens_from(usage_raw, "completion_tokens_details"),
        )
        carries = True

    choices = data.get("choices") or []
    if choices:
        choice = choices[0]
        if choice.get("finish_reason"):
            chunk.stop_reason = str(choice["finish_reason"])
            carries = True
        delta = choice.get("delta") or {}
        if delta.get("content"):
            chunk.text = str(delta["content"])
            carries = True
        if reasoning := reasoning_field(delta):
            chunk.reasoning = reasoning
            carries = True
        for raw in delta.get("tool_calls") or []:
            fn = raw.get("function") or {}
            chunk.tool_calls.append(
                ToolCallDelta(
                    # Absent index means a single call: treat it as index 0 rather
                    # than dropping the fragment.
                    index=int(raw.get("index", 0) or 0),
                    id=raw.get("id"),
                    name=fn.get("name"),
                    arguments_fragment=str(fn.get("arguments") or ""),
                )
            )
            carries = True

    return chunk if carries else None


async def stream_openai_sse(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[CompletionChunk]:
    """Stream an OpenAI-compatible chat completion as neutral chunks."""
    body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    owned = client is None
    c = client or httpx.AsyncClient(timeout=180.0)
    # Inline <think> reasoning arrives inside `content`, possibly with a tag
    # split across two chunks; the splitter moves it to `reasoning`.
    think = ThinkTagSplitter()
    try:
        async with c.stream("POST", url, json=body, headers=headers) as resp:
            await araise_for_status_with_body(resp)
            async for line in resp.aiter_lines():
                line = line.strip()
                # Comments (": keepalive") and blank separators carry no payload.
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == _DONE:
                    break
                try:
                    data = json.loads(data_str)
                except (ValueError, TypeError):
                    # A provider quirk must not cost the run; skip the line.
                    logger.debug("skipping unparseable stream line: %r", data_str[:120])
                    continue
                if not isinstance(data, dict):
                    continue
                chunk = _chunk_from_openai(data)
                if chunk is None:
                    continue
                if chunk.text:
                    chunk.text, inline = think.feed(chunk.text)
                    chunk.reasoning += inline
                yield chunk
        held_text, held_reasoning = think.flush()
        if held_text or held_reasoning:
            yield CompletionChunk(text=held_text, reasoning=held_reasoning)
    finally:
        if owned:
            await c.aclose()


def chunk_from_result(result: CompletionResult) -> CompletionChunk:
    """A whole CompletionResult, reshaped as the one chunk a non-streaming
    caller would have produced -- shared by stream_with_fallback's own
    fallback path below and by test doubles that implement `.complete()`
    but not `.stream()`, so both stay a single true synthesis instead of
    two copies drifting apart."""
    return CompletionChunk(
        text=result.text,
        reasoning=result.reasoning or "",
        replay_blocks=list(result.replay_blocks),
        tool_calls=[
            ToolCallDelta(
                index=i,
                id=tc.id,
                name=tc.name,
                # Complete, not a fragment -- there is nothing to continue.
                arguments_fragment=json.dumps(tc.arguments),
            )
            for i, tc in enumerate(result.tool_calls)
        ],
        usage=result.usage,
        stop_reason=result.stop_reason,
        provider=result.provider,
        model=result.model,
    )


async def stream_with_fallback(
    adapter: Any, req: CompletionRequest
) -> AsyncIterator[CompletionChunk]:
    """Stream from ``adapter``, or synthesise a single chunk if it cannot stream.

    The fallback keeps every provider usable through the gateway: a harness gets
    one chunk with the complete answer instead of an error.
    """
    stream = getattr(adapter, "stream", None)
    if callable(stream):
        async for chunk in stream(req):
            yield chunk
        return

    result: CompletionResult = await adapter.complete(req)
    yield chunk_from_result(result)
