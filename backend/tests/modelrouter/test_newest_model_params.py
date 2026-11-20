"""The newest OpenAI and Claude models refuse parameters older ones took.

OpenAI's reasoning models (o-series, GPT-5) take `max_completion_tokens`, not
`max_tokens`, and no `temperature`. Claude from Opus 4.7 on refuses
`temperature`, always thinks, reports a spent answer budget as `max_tokens`
and may decline a request with `refusal`.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx
import pytest

from oc8.modelrouter.adapters._openai_common import build_payload
from oc8.modelrouter.adapters.anthropic import AnthropicAdapter, _build_payload
from oc8.modelrouter.adapters.openai import OpenAIAdapter
from oc8.modelrouter.adapters.openai_compatible import OpenAICompatibleAdapter
from oc8.modelrouter.reasoning_support import anthropic_takes_temperature
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage

pytestmark = pytest.mark.asyncio

_OK = {
    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
}
# The bodies api.openai.com answers with (OpenAI API reference, error shapes).
_MAX_TOKENS_REFUSED = {
    "error": {
        "message": (
            "Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead."
        ),
        "type": "invalid_request_error",
        "param": "max_tokens",
        "code": "unsupported_parameter",
    }
}
_TEMPERATURE_REFUSED = {
    "error": {
        "message": (
            "Unsupported value: 'temperature' does not support 0.3 with this model. "
            "Only the default (1) value is supported."
        ),
        "type": "invalid_request_error",
        "param": "temperature",
        "code": "unsupported_value",
    }
}


def _req(provider: str, model: str) -> CompletionRequest:
    return CompletionRequest(
        provider=provider,
        model=model,
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(temperature=0.3, max_tokens=64),
    )


def _post_answering(monkeypatch: pytest.MonkeyPatch, *bodies: tuple[int, Any]) -> list[Any]:
    """`httpx.AsyncClient.post` answers with `bodies` in order (the last one
    repeats); returns the list of payloads it was sent."""
    sent: list[Any] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        sent.append(json)
        status, body = bodies[min(len(sent), len(bodies)) - 1]
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return sent


class _FakeStream:
    def __init__(self, status: int, body: bytes) -> None:
        self._resp = httpx.Response(
            status,
            content=body,
            headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://example.test/v1"),
        )

    async def __aenter__(self) -> httpx.Response:
        return self._resp

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _stream_answering(monkeypatch: pytest.MonkeyPatch, *bodies: tuple[int, bytes]) -> list[Any]:
    sent: list[Any] = []

    def fake_stream(self: httpx.AsyncClient, method: str, url: str, **kw: Any) -> _FakeStream:
        sent.append(kw.get("json"))
        status, body = bodies[min(len(sent), len(bodies)) - 1]
        return _FakeStream(status, body)

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    return sent


_OPENAI_SSE = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


# ------------------------------------------------------------------- OpenAI


async def test_openai_sends_max_completion_tokens_and_no_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _post_answering(monkeypatch, (200, _OK))
    await OpenAIAdapter(api_key="sk-test").complete(_req("openai", "gpt-4o"))
    assert sent[0]["max_completion_tokens"] == 64
    assert "max_tokens" not in sent[0]
    # An older model still takes its temperature.
    assert sent[0]["temperature"] == 0.3


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5.2-mini", "o3", "o4-mini", "openai/o3"])
async def test_openai_sends_a_reasoning_model_no_temperature(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    sent = _post_answering(monkeypatch, (200, _OK))
    await OpenAIAdapter(api_key="sk-test").complete(_req("openai", model))
    assert "temperature" not in sent[0]
    assert sent[0]["max_completion_tokens"] == 64


async def test_a_gateway_keeps_max_tokens_and_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vLLM, LiteLLM and other OpenAI-compatible servers know `max_tokens`."""
    sent = _post_answering(monkeypatch, (200, _OK))
    adapter = OpenAICompatibleAdapter(base_url="https://gw.test/v1")
    await adapter.complete(_req("openai_compatible", "gpt-5"))
    assert sent[0]["max_tokens"] == 64 and sent[0]["temperature"] == 0.3
    assert "max_completion_tokens" not in sent[0]
    assert build_payload(_req("openai_compatible", "gpt-5"))["max_tokens"] == 64


async def test_an_operators_max_completion_tokens_only_reaches_a_gateway() -> None:
    req = _req("openai", "gpt-5")
    req.params.extra = {"max_completion_tokens": 9}
    assert build_payload(req, openai_api=True)["max_completion_tokens"] == 64
    assert build_payload(req)["max_completion_tokens"] == 9


async def test_a_gateway_retries_once_with_max_completion_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _post_answering(monkeypatch, (400, _MAX_TOKENS_REFUSED), (200, _OK))
    adapter = OpenAICompatibleAdapter(base_url="https://gw.test/v1")
    result = await adapter.complete(_req("openai_compatible", "gpt-5"))
    assert result.text == "hi"
    assert len(sent) == 2
    assert "max_tokens" not in sent[1] and sent[1]["max_completion_tokens"] == 64


async def test_a_refused_temperature_is_retried_once_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-4.5-next: a name the "can think" rule does not know.
    sent = _post_answering(monkeypatch, (400, _TEMPERATURE_REFUSED), (200, _OK))
    result = await OpenAIAdapter(api_key="sk-test").complete(_req("openai", "gpt-4.5-next"))
    assert result.text == "hi"
    assert "temperature" in sent[0] and "temperature" not in sent[1]


async def test_both_refusals_in_a_row_are_each_answered_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _post_answering(
        monkeypatch, (400, _MAX_TOKENS_REFUSED), (400, _TEMPERATURE_REFUSED), (200, _OK)
    )
    adapter = OpenAICompatibleAdapter(base_url="https://gw.test/v1")
    result = await adapter.complete(_req("openai_compatible", "some-new-model"))
    assert result.text == "hi" and len(sent) == 3
    assert "max_tokens" not in sent[2] and "temperature" not in sent[2]


async def test_an_unrelated_400_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _post_answering(monkeypatch, (400, {"error": {"message": "invalid api key format"}}))
    with pytest.raises(httpx.HTTPStatusError, match="invalid api key format"):
        await OpenAIAdapter(api_key="sk-test").complete(_req("openai", "gpt-4o"))
    assert len(sent) == 1


async def test_a_refused_parameter_that_stays_refused_is_not_retried_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _post_answering(monkeypatch, (400, _TEMPERATURE_REFUSED))
    with pytest.raises(httpx.HTTPStatusError):
        await OpenAIAdapter(api_key="sk-test").complete(_req("openai", "gpt-4.5-next"))
    assert len(sent) == 2


async def test_the_stream_retries_a_refused_max_tokens_before_anything_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _stream_answering(
        monkeypatch, (400, jsonlib.dumps(_MAX_TOKENS_REFUSED).encode()), (200, _OPENAI_SSE)
    )
    adapter = OpenAICompatibleAdapter(base_url="https://gw.test/v1")
    chunks = [c async for c in adapter.stream(_req("openai_compatible", "gpt-5"))]
    assert "".join(c.text for c in chunks) == "hi"
    assert len(sent) == 2
    assert "max_tokens" not in sent[1] and sent[1]["max_completion_tokens"] == 64


async def test_the_openai_stream_sends_max_completion_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _stream_answering(monkeypatch, (200, _OPENAI_SSE))
    chunks = [c async for c in OpenAIAdapter(api_key="sk-test").stream(_req("openai", "gpt-5"))]
    assert "".join(c.text for c in chunks) == "hi"
    assert sent[0]["max_completion_tokens"] == 64
    assert "max_tokens" not in sent[0] and "temperature" not in sent[0]


# ---------------------------------------------------------------- Anthropic


@pytest.mark.parametrize(
    ("model", "takes"),
    [
        ("claude-3-5-sonnet-20241022", True),
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-6", True),
        ("claude-opus-4-6", True),
        ("claude-opus-4-7", False),
        ("claude-opus-4-8", False),
        ("claude-sonnet-5", False),
        ("claude-opus-5", False),
        ("claude-opus-5-5", False),
        ("claude-sonnet-5-5", False),
        ("claude-haiku-5-5", False),
        ("claude-fable-5-2", False),
        ("claude-mythos-5-2", False),
        ("anthropic/claude-opus-5-5", False),
    ],
)
def test_which_claude_models_take_a_temperature(model: str, takes: bool) -> None:
    assert anthropic_takes_temperature(model) is takes
    assert ("temperature" in _build_payload(_req("anthropic", model))) is takes


async def test_any_400_naming_temperature_is_retried_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety net behind the version rule: whatever the wording."""
    refused = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "`temperature` may only be set to 1 when thinking is enabled.",
        },
    }
    answered = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}
    sent = _post_answering(monkeypatch, (400, refused), (200, answered))
    result = await AnthropicAdapter(api_key="sk-ant-test").complete(
        _req("anthropic", "claude-sonnet-4-6")
    )
    assert result.text == "ok"
    assert "temperature" in sent[0] and "temperature" not in sent[1]


async def test_a_spent_answer_budget_reads_as_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtimes' one retry with a bigger budget looks for "length"."""
    _post_answering(monkeypatch, (200, {"content": [], "stop_reason": "max_tokens", "usage": {}}))
    result = await AnthropicAdapter(api_key="sk-ant-test").complete(
        _req("anthropic", "claude-opus-5-5")
    )
    assert result.stop_reason == "length"


@pytest.mark.parametrize("raw", ["end_turn", "tool_use", "refusal", "pause_turn"])
async def test_every_other_stop_reason_passes_through(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    _post_answering(monkeypatch, (200, {"content": [], "stop_reason": raw, "usage": {}}))
    result = await AnthropicAdapter(api_key="sk-ant-test").complete(
        _req("anthropic", "claude-opus-5-5")
    )
    assert result.stop_reason == raw


async def test_the_stream_reads_a_spent_answer_budget_as_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        b"event: message_delta\n"
        b'data: {"delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":64}}\n\n'
    )
    _stream_answering(monkeypatch, (200, body))
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    chunks = [c async for c in adapter.stream(_req("anthropic", "claude-opus-5-5"))]
    assert chunks[-1].stop_reason == "length"
