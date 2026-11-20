"""Newer Claude models (claude-sonnet-5, claude-opus-5, ...) reject
`temperature` outright: 400 invalid_request_error, "temperature is deprecated
for this model." The adapter leaves the field out for the versions known to
do this (`anthropic_takes_temperature`); this retry is the safety net behind
that rule, for a model it still sends the field to: it reads the refusal from
the provider's own words and tries once more without the field.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from oc8.modelrouter.adapters.anthropic import AnthropicAdapter
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage

pytestmark = pytest.mark.asyncio

# Live, 2026-09: the real body from api.anthropic.com for claude-sonnet-5.
_DEPRECATED_BODY = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "temperature is deprecated for this model.",
    },
}


def _req(model: str = "claude-sonnet-4-6") -> CompletionRequest:
    return CompletionRequest(
        provider="anthropic",
        model=model,
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(temperature=0.3, max_tokens=64),
    )


async def test_complete_retries_once_without_temperature_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        if len(calls) == 1:
            return httpx.Response(400, json=_DEPRECATED_BODY, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hi there"}],
                "stop_reason": "end_turn",
                "usage": {},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    result = await adapter.complete(_req())

    assert result.text == "hi there"
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]


async def test_complete_does_not_retry_an_unrelated_400(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            400,
            json={"error": {"message": "max_tokens: 200000 > 8192"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    with pytest.raises(httpx.HTTPStatusError, match="max_tokens: 200000 > 8192"):
        await adapter.complete(_req())

    assert len(calls) == 1


def _sse(*events: tuple[str, str]) -> bytes:
    out = b""
    for event, data in events:
        out += f"event: {event}\ndata: {data}\n\n".encode()
    return out


class _FakeStream:
    def __init__(self, resp: httpx.Response) -> None:
        self._resp = resp

    async def __aenter__(self) -> httpx.Response:
        return self._resp

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def test_stream_retries_once_without_temperature_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_stream(
        self: httpx.AsyncClient, method: str, url: str, json: Any = None, **kw: Any
    ) -> _FakeStream:
        calls.append(json)
        request = httpx.Request(method, url)
        if len(calls) == 1:
            return _FakeStream(httpx.Response(400, json=_DEPRECATED_BODY, request=request))
        body = _sse(
            ("content_block_delta", '{"index":0,"delta":{"type":"text_delta","text":"ok"}}'),
            ("message_delta", '{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}'),
        )
        return _FakeStream(
            httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}, request=request
            )
        )

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    chunks = [c async for c in adapter.stream(_req())]

    assert "".join(c.text for c in chunks) == "ok"
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]


async def test_stream_does_not_retry_an_unrelated_400(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_stream(
        self: httpx.AsyncClient, method: str, url: str, json: Any = None, **kw: Any
    ) -> _FakeStream:
        calls.append(json)
        return _FakeStream(
            httpx.Response(
                529,
                json={"error": {"message": "overloaded"}},
                request=httpx.Request(method, url),
            )
        )

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    with pytest.raises(httpx.HTTPStatusError, match="overloaded"):
        async for _chunk in adapter.stream(_req()):
            pass

    assert len(calls) == 1
