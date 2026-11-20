"""`ModelParams.extra` (free-form "Advanced/Raw parameters") is merged into
each adapter's outbound payload verbatim, strictly after every named field --
an operator-typed key that collides with one oc8 already sets (model,
messages, tools, ...) must be dropped, never allowed to clobber it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from oc8.modelrouter.adapters._openai_common import build_payload
from oc8.modelrouter.adapters.anthropic import AnthropicAdapter
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage

pytestmark = pytest.mark.asyncio

_OK_BODY = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}


def _req(extra: dict[str, Any] | None) -> CompletionRequest:
    return CompletionRequest(
        provider="anthropic",
        model="claude-sonnet-5",
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(temperature=0.3, max_tokens=64, extra=extra),
    )


async def test_anthropic_forwards_extra_keys_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json=_OK_BODY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    await adapter.complete(_req({"top_p": 0.9}))

    assert calls[0]["top_p"] == 0.9


async def test_anthropic_drops_an_extra_key_that_collides_with_a_reserved_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json=_OK_BODY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    await adapter.complete(_req({"model": "not-the-real-model", "messages": "nope"}))

    assert calls[0]["model"] == "claude-sonnet-5"
    assert calls[0]["messages"] != "nope"


async def test_no_extra_configured_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, json: Any = None, **kw: Any
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json=_OK_BODY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    await adapter.complete(_req(None))

    # claude-sonnet-5 refuses `temperature`, so it is not sent at all.
    assert set(calls[0]) == {"model", "max_tokens", "messages"}


def _openai_req(extra: dict[str, Any] | None) -> CompletionRequest:
    return CompletionRequest(
        provider="openai",
        model="gpt-5",
        messages=[NeutralMessage(role="user", content="hi")],
        params=ModelParams(temperature=0.3, max_tokens=64, extra=extra),
    )


def test_openai_common_forwards_extra_keys_verbatim() -> None:
    payload = build_payload(_openai_req({"top_p": 0.9, "provider": {"order": ["a"]}}))

    assert payload["top_p"] == 0.9
    assert payload["provider"] == {"order": ["a"]}


def test_openai_common_drops_an_extra_key_that_collides_with_a_reserved_field() -> None:
    payload = build_payload(_openai_req({"temperature": 1.9, "model": "not-the-real-model"}))

    assert payload["temperature"] == 0.3
    assert payload["model"] == "gpt-5"


def test_openai_common_no_extra_configured_adds_nothing() -> None:
    payload = build_payload(_openai_req(None))

    assert set(payload) == {"model", "messages", "temperature", "max_tokens"}
