"""Prompt caching for Claude: every request asks Anthropic to cache its prefix
(top-level `cache_control`), and the usage that comes back splits the prompt
into uncached, written and read tokens. `tokens_in` stays the full prompt so
budgets keep their meaning; the cached parts ride beside it."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from oc8.modelrouter.accumulate import accumulate_stream
from oc8.modelrouter.adapters.anthropic import AnthropicAdapter
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage

pytestmark = pytest.mark.asyncio

_CACHED_USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 30000,
    "output_tokens": 7,
}


def _req(extra: dict[str, Any] | None = None) -> CompletionRequest:
    return CompletionRequest(
        provider="anthropic",
        model="claude-sonnet-4-5",
        messages=[
            NeutralMessage(role="system", content="be brief"),
            NeutralMessage(role="user", content="hi"),
        ],
        params=ModelParams(temperature=0.3, max_tokens=64, extra=extra),
    )


def _patch_post(monkeypatch: pytest.MonkeyPatch, usage: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        body = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        return httpx.Response(
            200, json={**body, "usage": usage}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def _patch_stream(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _FakeStream:
        async def __aenter__(self) -> httpx.Response:
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            )

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def fake_stream(self: httpx.AsyncClient, method: str, url: str, **kw: Any) -> _FakeStream:
        calls.append(kw["json"])
        return _FakeStream()

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    return calls


def _sse(*events: tuple[str, str]) -> bytes:
    return b"".join(f"event: {e}\ndata: {d}\n\n".encode() for e, d in events)


async def test_complete_asks_for_the_prefix_to_be_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(monkeypatch, {})
    await AnthropicAdapter(api_key="k").complete(_req())
    assert calls[0]["cache_control"] == {"type": "ephemeral"}


async def test_stream_asks_for_the_prefix_to_be_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_stream(monkeypatch, _sse(("message_stop", "{}")))
    [c async for c in AnthropicAdapter(api_key="k").stream(_req())]
    assert calls[0]["cache_control"] == {"type": "ephemeral"}


async def test_caching_switched_off_sends_no_cache_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_post(monkeypatch, {})
    await AnthropicAdapter(api_key="k", prompt_caching=False).complete(_req())
    stream_calls = _patch_stream(monkeypatch, _sse(("message_stop", "{}")))
    [c async for c in AnthropicAdapter(api_key="k", prompt_caching=False).stream(_req())]
    assert "cache_control" not in calls[0]
    assert "cache_control" not in stream_calls[0]


async def test_an_operator_extra_cannot_override_cache_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_post(monkeypatch, {})
    await AnthropicAdapter(api_key="k", prompt_caching=False).complete(
        _req({"cache_control": {"type": "ephemeral", "ttl": "1h"}})
    )
    assert "cache_control" not in calls[0]


async def test_complete_reports_the_full_prompt_and_its_cached_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(monkeypatch, _CACHED_USAGE)
    result = await AnthropicAdapter(api_key="k").complete(_req())
    assert result.usage.tokens_in == 32100
    assert result.usage.cache_write_tokens == 2000
    assert result.usage.cache_read_tokens == 30000
    assert result.usage.tokens_out == 7


async def test_stream_reports_the_full_prompt_and_its_cached_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_usage = (
        '{"input_tokens":100,"cache_creation_input_tokens":2000,'
        '"cache_read_input_tokens":30000,"output_tokens":1}'
    )
    body = _sse(
        ("message_start", '{"message":{"usage":' + start_usage + "}}"),
        ("content_block_start", '{"index":0,"content_block":{"type":"text","text":""}}'),
        ("content_block_delta", '{"index":0,"delta":{"type":"text_delta","text":"ok"}}'),
        ("message_delta", '{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}'),
        ("message_stop", "{}"),
    )
    _patch_stream(monkeypatch, body)
    result = await accumulate_stream(AnthropicAdapter(api_key="k").stream(_req()))
    assert result.usage.tokens_in == 32100
    assert result.usage.cache_write_tokens == 2000
    assert result.usage.cache_read_tokens == 30000
    assert result.usage.tokens_out == 7


async def test_a_response_without_cache_fields_reports_zero_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post(monkeypatch, {"input_tokens": 50, "output_tokens": 3})
    result = await AnthropicAdapter(api_key="k").complete(_req())
    assert result.usage.tokens_in == 50
    assert result.usage.cache_read_tokens == 0
    assert result.usage.cache_write_tokens == 0
