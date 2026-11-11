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
    assert len(adapter.requests) == 2
    assert adapter.requests[1].params.reasoning is None


async def test_an_unrelated_400_on_a_stream_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FailsOnce(_rejection('{"error": "messages: field required"}'))
    with pytest.raises(httpx.HTTPStatusError):
        await accumulate_stream(_router(monkeypatch, adapter).stream(_req()))
    assert len(adapter.requests) == 1


async def test_a_failing_retry_is_not_retried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AlwaysRefuses(_FailsOnce):
        async def complete(self, req: CompletionRequest) -> CompletionResult:
            self.requests.append(req)
            raise self.error

    adapter = _AlwaysRefuses(_rejection(_REFUSED))
    with pytest.raises(httpx.HTTPStatusError):
        await _router(monkeypatch, adapter).complete(_req())
    assert len(adapter.requests) == 2


async def test_a_retry_that_dies_before_its_first_chunk_leaves_fallback_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag rides the retry's first real chunk, not a marker of its own."""

    class _RefusesThenFlaps:
        provider = "openai"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, req: CompletionRequest) -> CompletionResult:  # pragma: no cover
            raise AssertionError("stream only")

        async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
            self.calls += 1
            if self.calls == 1:
                raise _rejection(_REFUSED)
            raise _rejection("upstream down", status=503)
            yield  # pragma: no cover

    adapter = _RefusesThenFlaps()
    seen: list[CompletionChunk] = []
    with pytest.raises(httpx.HTTPStatusError):
        async for chunk in _router(monkeypatch, adapter).stream(_req()):
            seen.append(chunk)
    assert seen == []  # nothing emitted, so stream_completion_with_fallback may move on


async def test_the_flag_rides_the_first_real_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FailsOnce(_rejection(_REFUSED))
    chunks = [c async for c in _router(monkeypatch, adapter).stream(_req())]
    assert [c.text for c in chunks] == ["ok"]
    assert chunks[0].reasoning_dropped is True


@pytest.mark.parametrize("status", [401, 402, 403, 404, 413, 429])
def test_other_statuses_are_never_a_refused_parameter(status: int) -> None:
    assert rejects_reasoning(_rejection('{"error": "model think-v2 not found"}', status)) is False


def test_the_url_is_not_read_as_the_providers_words() -> None:
    request = httpx.Request("POST", "https://reasoning.example/effort/think")
    response = httpx.Response(400, request=request, text='{"error": "bad json"}')
    exc = httpx.HTTPStatusError("400", request=request, response=response)
    assert rejects_reasoning(exc) is False


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
