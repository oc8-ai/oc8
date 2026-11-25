# backend/src/oc8/modelrouter/adapters/openai.py
"""OpenAI adapter -- Chat Completions API with tool use, and the Responses API
for the models that reason next to function tools only there.

gpt-6.2-sol and gpt-6-astra refuse `reasoning_effort` next to function tools
on Chat Completions ("use /v1/responses or set reasoning_effort to 'none'").
With a reasoning level asked for, the request moves to the Responses API and
keeps its level; the model is remembered for the life of the process, so its
next call goes there straight away. Without a level, Chat Completions stays
and gets `reasoning_effort: "none"` (see `_openai_common.adapt_refused_payload`).
The Responses wire format is the one `chatgpt_subscription` already speaks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from oc8.modelrouter.adapters._openai_common import (
    asks_for_responses_api,
    build_payload,
    parse_response,
    post_completion,
    stream_completion,
)
from oc8.modelrouter.adapters.chatgpt_subscription import (
    build_responses_payload,
    parse_responses_result,
    post_responses,
    stream_responses_sse,
)
from oc8.modelrouter.types import CompletionChunk, CompletionRequest, CompletionResult

_API_URL = "https://api.openai.com/v1/chat/completions"
_RESPONSES_URL = "https://api.openai.com/v1/responses"

#: Models that asked for the Responses API to reason next to tools. Per
#: process: a restart costs one refused call per model, nothing more.
_RESPONSES_MODELS: set[str] = set()


def _wants_responses(req: CompletionRequest) -> bool:
    return bool(req.tools) and req.params.reasoning is not None


def _responses_payload(req: CompletionRequest) -> dict[str, Any]:
    payload = build_responses_payload(req)
    payload["max_output_tokens"] = req.params.max_tokens
    cache_key = (req.params.extra or {}).get("prompt_cache_key")
    if cache_key:
        payload["prompt_cache_key"] = cache_key
    return payload


class OpenAIAdapter:
    provider = "openai"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "content-type": "application/json"}

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        wants = _wants_responses(req)
        if wants and req.model in _RESPONSES_MODELS:
            return await self._complete_responses(req)
        try:
            data = await post_completion(
                _API_URL,
                build_payload(req, openai_api=True),
                self._headers(),
                reasoning_none=not wants,
            )
        except httpx.HTTPStatusError as exc:
            if not (wants and asks_for_responses_api(exc)):
                raise
            _RESPONSES_MODELS.add(req.model)
            return await self._complete_responses(req)
        return parse_response(data, provider=self.provider, model=req.model)

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        wants = _wants_responses(req)
        if wants and req.model in _RESPONSES_MODELS:
            async for chunk in self._stream_responses(req):
                yield chunk
            return
        emitted = False
        try:
            async for chunk in stream_completion(
                _API_URL,
                build_payload(req, openai_api=True),
                self._headers(),
                reasoning_none=not wants,
            ):
                emitted = True
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            # A refusal arrives before any line; once a chunk is out, the
            # error is the caller's.
            if emitted or not (wants and asks_for_responses_api(exc)):
                raise
            _RESPONSES_MODELS.add(req.model)
        async for chunk in self._stream_responses(req):
            yield chunk

    async def _complete_responses(self, req: CompletionRequest) -> CompletionResult:
        data = await post_responses(_RESPONSES_URL, _responses_payload(req), self._headers())
        return parse_responses_result(data, provider=self.provider, model=req.model)

    async def _stream_responses(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        async for chunk in stream_responses_sse(
            _RESPONSES_URL, _responses_payload(req), self._headers()
        ):
            yield chunk
