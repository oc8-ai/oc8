"""A provider that refuses the reasoning parameter still answers: one retry
without it, recorded on the step (spec 2026-11-11 §9)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from oc8.agent.harness.step_timing import note_reasoning
from oc8.config import get_settings
from oc8.modelrouter.accumulate import accumulate_stream
from oc8.modelrouter.router import ModelRouter, rejects_reasoning
from oc8.modelrouter.types import (
    CompletionChunk,
    CompletionRequest,
    CompletionResult,
    ModelParams,
    NeutralMessage,
    Usage,
)
from oc8.runtime.step_record import step_timing_dto

_REFUSED = '{"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}}'


def _rejection(body: str, status: int = 400) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(
        f"{status} from {request.url}: {body}", request=request, response=response
    )


def _req(params: ModelParams | None = None) -> CompletionRequest:
    return CompletionRequest(
        provider="openai",
        model="o3",
        api_key="sk-test",
        messages=[NeutralMessage(role="user", content="hi")],
        params=params or ModelParams(reasoning="thorough", effort="high"),
    )


class _FailsOnce:
    provider = "openai"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.requests: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        self.requests.append(req)
        if len(self.requests) == 1:
            raise self.error
        return CompletionResult(
            text="ok",
            tool_calls=[],
            usage=Usage(1, 1),
            stop_reason="stop",
            provider="openai",
            model=req.model,
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        self.requests.append(req)
        if len(self.requests) == 1:
            raise self.error
        yield CompletionChunk(text="ok")


def _router(monkeypatch: pytest.MonkeyPatch, adapter: Any) -> ModelRouter:
    router = ModelRouter(get_settings())
    monkeypatch.setattr(router, "_adapter", lambda *a, **k: adapter)
    return router


def test_rejects_reasoning_reads_the_providers_words() -> None:
    assert rejects_reasoning(_rejection(_REFUSED)) is True
    assert rejects_reasoning(_rejection('{"error": "think value not supported"}')) is True
    assert rejects_reasoning(_rejection('{"error": "messages: field required"}')) is False
    assert rejects_reasoning(_rejection(_REFUSED, status=429)) is False
    assert rejects_reasoning(_rejection(_REFUSED, status=500)) is False
    assert rejects_reasoning(ValueError("reasoning")) is False


async def test_complete_retries_once_without_the_level(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FailsOnce(_rejection(_REFUSED))
    result = await _router(monkeypatch, adapter).complete(_req())
    assert result.text == "ok" and result.reasoning_dropped is True
    first, second = adapter.requests
    assert first.params.reasoning == "thorough"
    assert (second.params.reasoning, second.params.effort) == (None, None)


async def test_an_unrelated_400_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FailsOnce(_rejection('{"error": "messages: field required"}'))
    with pytest.raises(httpx.HTTPStatusError):
        await _router(monkeypatch, adapter).complete(_req())
    assert len(adapter.requests) == 1


async def test_a_rate_limit_is_not_retried_without_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FailsOnce(_rejection(_REFUSED, status=429))
    with pytest.raises(httpx.HTTPStatusError):
        await _router(monkeypatch, adapter).complete(_req())
    assert len(adapter.requests) == 1


async def test_a_request_without_a_level_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FailsOnce(_rejection(_REFUSED))
    with pytest.raises(httpx.HTTPStatusError):
        await _router(monkeypatch, adapter).complete(_req(ModelParams()))
    assert len(adapter.requests) == 1


async def test_stream_retries_once_and_marks_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FailsOnce(_rejection(_REFUSED))
    result = await accumulate_stream(_router(monkeypatch, adapter).stream(_req()))
    assert result.text == "ok" and result.reasoning_dropped is True
    assert adapter.requests[1].params.reasoning is None


class _AnswersThenDies:
    provider = "openai"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: CompletionRequest) -> CompletionResult:  # pragma: no cover
        raise AssertionError("stream only")

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        self.calls += 1
        yield CompletionChunk(text="partial")
        raise _rejection(_REFUSED)


async def test_a_stream_that_already_answered_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _AnswersThenDies()
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in _router(monkeypatch, adapter).stream(_req()):
            pass
    assert adapter.calls == 1


def test_the_step_records_it_and_the_wire_carries_it() -> None:
    record: dict[str, Any] = {"step": 1}
    note_reasoning(record, None, dropped=True)
    assert record["reasoning_dropped"] is True
    assert step_timing_dto(record)["reasoningDropped"] is True
    plain: dict[str, Any] = {"step": 2}
    note_reasoning(plain, None)
    assert "reasoning_dropped" not in plain and "reasoningDropped" not in step_timing_dto(plain)
