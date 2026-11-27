"""A plugin provider's calls are named after the plugin, streamed or not.

A plugin that contributes a provider (say `opaas_ai`) usually builds its
adapter on the OpenAI-compatible one, whose own `provider` is
`openai_compatible`. `stream()` stamps every chunk with the provider the
router resolved, so a chat run's usage is recorded under `opaas_ai`; a
non-streamed call (a chat title, a checkpoint summary) used to return the
adapter's own name, so the same model's usage landed under
`openai_compatible` -- outside the plugin provider's budget and price rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from oc8.config import get_settings
from oc8.modelrouter import registry
from oc8.modelrouter.router import ModelRouter
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ModelParams,
    NeutralMessage,
    Usage,
)


class _CompatibleAdapter:
    provider = "openai_compatible"

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text="ok",
            tool_calls=[],
            usage=Usage(3, 1),
            stop_reason="stop",
            provider=self.provider,
            model=req.model,
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text="ok", usage=Usage(3, 1), provider=self.provider)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> ModelRouter:
    entry = registry.ProviderEntry(
        canonical="demo_ai",
        aliases=(),
        locality="cloud",
        factory=lambda _s, _b, _k: _CompatibleAdapter(),
        available=lambda _s, _k: True,
    )
    monkeypatch.setattr(registry, "_plugin_entries", lambda: {"demo_ai": entry})
    return ModelRouter(get_settings())


def _req() -> CompletionRequest:
    return CompletionRequest(
        provider="demo_ai",
        model="demo-gpt",
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(),
        api_key="sk-test",
    )


async def test_a_completion_is_named_after_the_plugin_provider(router: ModelRouter) -> None:
    result = await router.complete(_req())
    assert result.provider == "demo_ai"
    assert result.model == "demo-gpt"


async def test_a_stream_and_a_completion_name_the_same_provider(router: ModelRouter) -> None:
    streamed = [chunk.provider async for chunk in router.stream(_req())]
    completed = (await router.complete(_req())).provider
    assert set(streamed) == {completed}
