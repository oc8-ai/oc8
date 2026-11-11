"""A minimal ModelAdapter contributed by a plugin. Its models whose name
starts with "demo-think" can think, and the adapter echoes the reasoning
level it received; "demo-crash" makes the declaration raise."""

from __future__ import annotations

from typing import Any

from oc8.modelrouter.types import CompletionRequest, CompletionResult, Usage


class DemoAdapter:
    async def complete(self, req: CompletionRequest) -> CompletionResult:
        text = "hello from a plugin provider"
        if req.params.reasoning is not None:
            text = f"{text} (reasoning: {req.params.reasoning})"
        return CompletionResult(
            text=text,
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="end_turn",
            provider="demo-llm",
            model=req.model,
        )


def _thinks(model: str) -> bool:
    if model == "demo-crash":
        raise RuntimeError("a capa bug")
    return model.startswith("demo-think")


def register(contrib: Any) -> None:
    contrib.add_model_provider(
        canonical="demo-llm",
        locality="cloud",
        factory=lambda _settings, _base_url, _key: DemoAdapter(),
        available=lambda _settings, _key: True,
        aliases=("demo",),
        supports_reasoning=_thinks,
    )
