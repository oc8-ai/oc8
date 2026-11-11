"""Every model in a fallback chain is gated on its OWN supports_reasoning
(spec §5): the params were resolved for the primary."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from oc8 import models as m
from oc8.modelrouter.fallback import complete_with_fallback, stream_completion_with_fallback
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ModelParams,
    NeutralMessage,
    Usage,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _unavailable() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    return httpx.HTTPStatusError(
        "503", request=request, response=httpx.Response(503, request=request)
    )


class _FirstFails:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        self.requests.append(req)
        if len(self.requests) == 1:
            raise _unavailable()
        return CompletionResult(
            text="ok",
            tool_calls=[],
            usage=Usage(1, 1),
            stop_reason="stop",
            provider=req.provider,
            model=req.model,
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        self.requests.append(req)
        if len(self.requests) == 1:
            raise _unavailable()
        yield CompletionChunk(text="ok")


async def _chain(db: Any, tenant: uuid.UUID) -> m.ModelConfig:
    fallback = m.ModelConfig(
        tenant_id=tenant, provider="openai", model="gpt-4o", supports_reasoning=False
    )
    db.add(fallback)
    await db.flush()
    primary = m.ModelConfig(
        tenant_id=tenant,
        provider="openai",
        model="o3",
        supports_reasoning=True,
        fallbacks=[str(fallback.id)],
    )
    db.add(primary)
    await db.flush()
    return primary


_PARAMS = ModelParams(reasoning="thorough", effort="high")


async def test_a_fallback_model_that_cannot_think_gets_no_level(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    router = _FirstFails()
    async with app_session(tenant) as db:
        primary = await _chain(db, tenant)
        await complete_with_fallback(
            db,
            router,  # type: ignore[arg-type]
            tenant_id=tenant,
            agent_id=None,
            primary=primary,
            no_config_provider="openai",
            no_config_model="o3",
            messages=[NeutralMessage(role="user", content="hi")],
            tools=[],
            params=_PARAMS,
            request_id=uuid.uuid4(),
            contains_restricted=False,
        )
    first, second = router.requests
    assert (first.params.reasoning, first.params.effort) == ("thorough", "high")
    assert (second.params.reasoning, second.params.effort) == (None, None)


async def test_a_streamed_fallback_that_cannot_think_gets_no_level(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    router = _FirstFails()
    async with app_session(tenant) as db:
        primary = await _chain(db, tenant)
        chunks = [
            c
            async for c in stream_completion_with_fallback(
                db,
                router,  # type: ignore[arg-type]
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                primary=primary,
                no_config_provider="openai",
                no_config_model="o3",
                messages=[NeutralMessage(role="user", content="hi")],
                tools=[],
                params=_PARAMS,
                request_id=uuid.uuid4(),
                contains_restricted=False,
            )
        ]
    assert "".join(c.text for c in chunks) == "ok"
    assert router.requests[0].params.reasoning == "thorough"
    assert (router.requests[1].params.reasoning, router.requests[1].params.effort) == (None, None)


class _EmptyChunkThenDies:
    """The first model emits an empty flagged chunk, then fails retryably."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        self.calls += 1
        if self.calls == 1:
            yield CompletionChunk(reasoning_dropped=True)
            raise _unavailable()
        yield CompletionChunk(text="ok")


async def test_an_empty_chunk_does_not_block_the_fallback(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    router = _EmptyChunkThenDies()
    async with app_session(tenant) as db:
        primary = await _chain(db, tenant)
        chunks = [
            c
            async for c in stream_completion_with_fallback(
                db,
                router,  # type: ignore[arg-type]
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                primary=primary,
                no_config_provider="openai",
                no_config_model="o3",
                messages=[NeutralMessage(role="user", content="hi")],
                tools=[],
                params=_PARAMS,
                request_id=uuid.uuid4(),
                contains_restricted=False,
            )
        ]
    assert "".join(c.text for c in chunks) == "ok"
    assert any(c.reasoning_dropped for c in chunks)
