# backend/src/oc8/modelrouter/adapters/openai_compatible.py
"""Generic OpenAI-compatible adapter — same wire format as OpenAI's Chat
Completions API, but with a configurable base URL. Covers Mistral (which
exposes an OpenAI-compatible chat endpoint) and any other third-party or
self-hosted OpenAI-API-compatible server, without a near-duplicate
dedicated adapter per provider."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

import httpx

from oc8.modelrouter.adapters._openai_audio import (
    Transcript,
    open_speech_stream,
    post_transcription,
)
from oc8.modelrouter.adapters._openai_common import (
    build_payload,
    parse_response,
    post_completion,
    stream_completion,
)
from oc8.modelrouter.http_errors import raise_for_status_with_body
from oc8.modelrouter.types import CompletionChunk, CompletionRequest, CompletionResult

_EMBED_TIMEOUT_S = 60.0


class OpenAICompatibleAdapter:
    provider = "openai_compatible"

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str:
        return self._api_key

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        data = await post_completion(
            f"{self._base_url}/chat/completions", build_payload(req), self._headers()
        )
        return parse_response(data, provider=self.provider, model=req.model)

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        async for chunk in stream_completion(
            f"{self._base_url}/chat/completions", build_payload(req), self._headers()
        ):
            yield chunk

    async def embed(self, text: str, model: str) -> list[float]:
        vector, _tokens = await self.embed_with_usage(text, model)
        return vector

    async def embed_with_usage(self, text: str, model: str) -> tuple[list[float], int]:
        """`POST {base_url}/embeddings` -> (vector, prompt tokens).

        OpenAI's wire format, which LiteLLM, vLLM, Mistral and most
        self-hosted servers speak: `{"data": [{"embedding": [...]}],
        "usage": {"prompt_tokens": N}}`. A provider that omits `usage`
        reports 0 tokens rather than failing the call. A response with no
        vector raises `ValueError`, an HTTP failure `httpx.HTTPError`."""
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT_S) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": model, "input": text},
                headers=self._headers(),
            )
            raise_for_status_with_body(resp)
            data = resp.json()
        items = data.get("data") or []
        vector = items[0].get("embedding") if items and isinstance(items[0], dict) else None
        if not vector:
            raise ValueError(f"no embedding in the response for model {model!r}")
        usage = data.get("usage") or {}
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        return [float(x) for x in vector], tokens

    async def transcribe(
        self, audio: bytes | io.BytesIO, *, model: str, content_type: str, language: str | None
    ) -> Transcript:
        """`POST {base_url}/audio/transcriptions` (`_openai_audio`)."""
        return await post_transcription(
            self._base_url,
            self._api_key,
            audio,
            model=model,
            content_type=content_type,
            language=language,
        )

    async def open_speech(
        self, text: str, *, model: str, extra: dict[str, Any] | None
    ) -> AsyncIterator[bytes]:
        """`POST {base_url}/audio/speech`, streamed (`_openai_audio`)."""
        return await open_speech_stream(
            self._base_url, self._api_key, text, model=model, extra=extra
        )
