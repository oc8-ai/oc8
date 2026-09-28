"""Reconstruct a whole CompletionResult from a stream of chunks.

The engines (agent/engine.py, api/v1/internal_agent.py's /step) still need one
complete answer at the end of a turn -- department caching, tool-call salvage,
and budget metering all operate on the whole thing, not a chunk at a time.
Streaming only changes how that answer arrives: token by token, live, instead
of in one response. This module is the seam between the two: consume the
stream, publish each text fragment as it arrives (for the Live Log), and hand
back the same CompletionResult shape the rest of the run pipeline already
expects.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from oc8.modelrouter.types import CompletionChunk, CompletionResult, ToolCall, Usage

OnText = Callable[[str], Awaitable[None]]


@dataclass
class StreamTiming:
    ttft_ms: int | None = None
    model_wait_ms: int = 0


@dataclass
class _PendingCall:
    id: str | None = None
    name: str | None = None
    arguments_json: str = ""


@dataclass
class _Accumulator:
    text_parts: list[str] = field(default_factory=list)
    calls: dict[int, _PendingCall] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "stop"
    provider: str | None = None
    model: str | None = None

    def absorb(self, chunk: CompletionChunk) -> None:
        if chunk.text:
            self.text_parts.append(chunk.text)
        for delta in chunk.tool_calls:
            call = self.calls.setdefault(delta.index, _PendingCall())
            if delta.id:
                call.id = delta.id
            if delta.name:
                call.name = delta.name
            call.arguments_json += delta.arguments_fragment
        # A provider reports usage once it knows the final count -- typically
        # only on the last chunk it sends, and always cumulative rather than
        # incremental, so the most recently reported value is the correct
        # total rather than something to sum across chunks.
        if chunk.usage is not None:
            self.usage = chunk.usage
        if chunk.stop_reason is not None:
            self.stop_reason = chunk.stop_reason
        if chunk.provider is not None:
            self.provider = chunk.provider
        if chunk.model is not None:
            self.model = chunk.model

    def result(self) -> CompletionResult:
        tool_calls = [
            ToolCall(
                id=call.id or f"call_{index}",
                name=call.name or "",
                # A call with no arguments streams zero input_json_delta/no
                # arguments fragment at all (e.g. a no-arg tool) -- treat an
                # empty accumulated string as "{}", not a parse error.
                arguments=json.loads(call.arguments_json) if call.arguments_json.strip() else {},
            )
            for index, call in sorted(self.calls.items())
        ]
        return CompletionResult(
            text="".join(self.text_parts),
            tool_calls=tool_calls,
            usage=self.usage,
            stop_reason=self.stop_reason,
            provider=self.provider or "",
            model=self.model or "",
        )


async def accumulate_stream(
    chunks: AsyncIterator[CompletionChunk],
    *,
    on_text: OnText | None = None,
    timing: StreamTiming | None = None,
) -> CompletionResult:
    """Drain `chunks`, calling `on_text` with each text fragment as it
    arrives, and return the fully assembled CompletionResult once the
    stream ends."""
    started = time.monotonic() if timing is not None else None
    acc = _Accumulator()
    try:
        async for chunk in chunks:
            if timing is not None and timing.ttft_ms is None and chunk.text:
                timing.ttft_ms = int((time.monotonic() - started) * 1000)
            acc.absorb(chunk)
            if chunk.text and on_text is not None:
                await on_text(chunk.text)
        return acc.result()
    finally:
        if timing is not None:
            timing.model_wait_ms = int((time.monotonic() - started) * 1000)
