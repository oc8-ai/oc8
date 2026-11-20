# backend/src/oc8/modelrouter/adapters/openai.py
"""OpenAI adapter — Chat Completions API with tool use."""

from __future__ import annotations

from collections.abc import AsyncIterator

from oc8.modelrouter.adapters._openai_common import (
    build_payload,
    parse_response,
    post_completion,
    stream_completion,
)
from oc8.modelrouter.types import CompletionChunk, CompletionRequest, CompletionResult

_API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIAdapter:
    provider = "openai"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "content-type": "application/json"}

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        data = await post_completion(_API_URL, build_payload(req, openai_api=True), self._headers())
        return parse_response(data, provider=self.provider, model=req.model)

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        async for chunk in stream_completion(
            _API_URL, build_payload(req, openai_api=True), self._headers()
        ):
            yield chunk
